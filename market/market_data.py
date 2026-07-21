# market/market_data.py
from __future__ import annotations

import os
from collections import deque
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple, Optional

from utils.smart_deque import SmartDeque


@dataclass
class Tick:
    t: float
    price: float
    volume: float


class MarketDataBuffer:
    """
    Stores recent tick data per-symbol (timestamps, prices, volumes).

    Strategies should generally consume *prices-only* and *volumes-only* lists.
    Timestamped getters are available for components that need Δt (e.g. VolatilityScorer).
    """
    def __init__(
        self,
        maxlen_prices: int = 25,
        maxlen_volumes: int = 25,
        live_bar_lateness_grace_seconds: float | None = None,
        max_recent_trade_ids_per_symbol: int = 5000,
    ):
        self._lock = threading.Lock()

        # Internal storage keeps event timestamps.
        self._prices: Dict[str, SmartDeque] = {}
        self._volumes: Dict[str, SmartDeque] = {}

        # Rolling bars built directly from streamed trades.
        self._live_bars: Dict[
            int,
            Dict[str, SmartDeque],
        ] = {
            60: {},
            300: {},
        }

        self._maxlen_live_bars = 500

        self._maxlen_prices = int(
            maxlen_prices
        )

        self._maxlen_volumes = int(
            maxlen_volumes
        )

        if (
            live_bar_lateness_grace_seconds
            is None
        ):
            raw_grace = os.getenv(
                "LIVE_BAR_LATENESS_GRACE_SECONDS",
                "2.0",
            )

            try:
                live_bar_lateness_grace_seconds = (
                    float(raw_grace)
                )
            except Exception:
                live_bar_lateness_grace_seconds = 2.0

        self._live_bar_lateness_grace_seconds = max(
            0.0,
            float(
                live_bar_lateness_grace_seconds
                or 0.0
            ),
        )

        self._max_recent_trade_ids_per_symbol = max(
            100,
            int(
                max_recent_trade_ids_per_symbol
                or 5000
            ),
        )

        self._recent_trade_ids: Dict[
            str,
            deque,
        ] = {}

        self._recent_trade_id_sets: Dict[
            str,
            set,
        ] = {}

        self._out_of_order_tick_counts: Dict[
            str,
            int,
        ] = {}

        # Each entry is:
        # {
        #     "started_at_epoch": float,
        #     "ended_at_epoch": float | None,
        # }
        self._stream_connection_intervals: list[
            dict
        ] = []

        self._max_stream_connection_intervals = 200


    def _norm_symbol(self, symbol: str) -> str:
        return (symbol or "").upper().strip()


    def _ensure_symbol(self, symbol: str) -> None:
        if symbol not in self._prices:
            self._prices[symbol] = SmartDeque(maxlen=self._maxlen_prices)
        if symbol not in self._volumes:
            self._volumes[symbol] = SmartDeque(maxlen=self._maxlen_volumes)

        for bars_by_symbol in self._live_bars.values():
            if symbol not in bars_by_symbol:
                bars_by_symbol[symbol] = SmartDeque(maxlen=self._maxlen_live_bars)

        if symbol not in self._recent_trade_ids:
            self._recent_trade_ids[symbol] = (
                deque()
            )

        if symbol not in self._recent_trade_id_sets:
            self._recent_trade_id_sets[symbol] = (
                set()
            )

        if symbol not in self._out_of_order_tick_counts:
            self._out_of_order_tick_counts[symbol] = 0


    def _to_epoch_seconds(self, timestamp: Any) -> float:
        if timestamp is None:
            return time.time()

        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return float(timestamp.timestamp())

        try:
            return float(timestamp)
        except Exception:
            return time.time()


    @staticmethod
    def _bucket_start(timestamp: float, timeframe_seconds: int) -> int:
        timeframe_seconds = int(timeframe_seconds)
        return int(timestamp // timeframe_seconds) * timeframe_seconds


    @staticmethod
    def _bar_time(bucket_start: int) -> datetime:
        return datetime.fromtimestamp(bucket_start, tz=timezone.utc)

    @staticmethod
    def _normalise_trade_id(
        trade_id: Any,
    ) -> str | None:
        if trade_id is None:
            return None

        value = str(
            trade_id
        ).strip()

        return value or None


    @staticmethod
    def _trade_sort_key(
        *,
        event_timestamp: float,
        receipt_timestamp: float,
        trade_id: Any,
    ) -> tuple:
        """
        Order trades by market event time.

        When two trades have identical event timestamps, prefer Alpaca's
        trade ID when it is numeric. Otherwise preserve receipt order.
        """
        try:
            numeric_trade_id = int(
                trade_id
            )

            return (
                float(event_timestamp),
                0,
                numeric_trade_id,
                float(receipt_timestamp),
            )

        except Exception:
            return (
                float(event_timestamp),
                1,
                0,
                float(receipt_timestamp),
            )


    def _find_live_bar_locked(
        self,
        bars,
        bucket_start: int,
    ) -> dict | None:
        for bar in reversed(
            list(
                bars or []
            )
        ):
            current_bucket = int(
                bar.get(
                    "bucket_start",
                    0,
                )
                or 0
            )

            if current_bucket == bucket_start:
                return bar

            if current_bucket < bucket_start:
                break

        return None


    def _connection_covers_bucket_locked(
        self,
        *,
        bucket_start: float,
        bucket_end: float,
    ) -> bool:
        for interval in (
            self._stream_connection_intervals
        ):
            started_at = float(
                interval.get(
                    "started_at_epoch",
                    0.0,
                )
                or 0.0
            )

            ended_at = interval.get(
                "ended_at_epoch"
            )

            if (
                started_at
                <= bucket_start + 1e-6
                and (
                    ended_at is None
                    or float(ended_at)
                    >= bucket_end - 1e-6
                )
            ):
                return True

        return False


    def _capture_quality_for_bucket_locked(
        self,
        *,
        bucket_start: float,
        bucket_end: float,
    ) -> str:
        if self._connection_covers_bucket_locked(
            bucket_start=bucket_start,
            bucket_end=bucket_end,
        ):
            return "FULL_CAPTURE_CANDIDATE"

        if not self._stream_connection_intervals:
            return "CONTINUITY_UNKNOWN"

        connection_starts = [
            float(
                interval.get(
                    "started_at_epoch",
                    0.0,
                )
                or 0.0
            )
            for interval in (
                self._stream_connection_intervals
            )
            if float(
                interval.get(
                    "started_at_epoch",
                    0.0,
                )
                or 0.0
            )
            > 0
        ]

        if connection_starts:
            first_connection_start = min(
                connection_starts
            )

            if (
                bucket_start
                < first_connection_start
                < bucket_end
            ):
                return "PARTIAL_STARTUP_BAR"

        return "STREAM_INTERRUPTED"


    def _seal_bar_locked(
        self,
        bar: dict,
        *,
        sealed_at_epoch: float,
    ) -> None:
        if bool(
            bar.get(
                "sealed"
            )
        ):
            return

        bucket_start = float(
            bar.get(
                "bucket_start",
                0.0,
            )
            or 0.0
        )

        bucket_end = float(
            bar.get(
                "bucket_end",
                0.0,
            )
            or 0.0
        )

        capture_quality = (
            self._capture_quality_for_bucket_locked(
                bucket_start=bucket_start,
                bucket_end=bucket_end,
            )
        )

        if bool(
            bar.get(
                "late_created_after_seal"
            )
        ):
            capture_quality = (
                "LATE_CREATED_AFTER_SEAL"
            )

        bar.update({
            "sealed": True,
            "sealed_at_epoch": float(
                sealed_at_epoch
            ),
            "sealed_at": datetime.fromtimestamp(
                float(
                    sealed_at_epoch
                ),
                tz=timezone.utc,
            ),
            "capture_quality": (
                capture_quality
            ),
        })


    def _seal_mature_bars_locked(
        self,
        now_epoch: float,
    ) -> None:
        for bars_by_symbol in (
            self._live_bars.values()
        ):
            for bars in (
                bars_by_symbol.values()
            ):
                for bar in bars or []:
                    if bool(
                        bar.get(
                            "sealed"
                        )
                    ):
                        continue

                    eligible_at = float(
                        bar.get(
                            "strategy_eligible_at_epoch",
                            0.0,
                        )
                        or 0.0
                    )

                    if (
                        eligible_at > 0
                        and now_epoch
                        >= eligible_at
                    ):
                        self._seal_bar_locked(
                            bar,
                            sealed_at_epoch=(
                                now_epoch
                            ),
                        )


    def _remember_trade_id_locked(
        self,
        symbol: str,
        trade_id: Any,
    ) -> bool:
        """
        Return False when this symbol/trade ID was already processed.

        When no trade ID is available, deterministic deduplication is not
        attempted because timestamp/price/size may legitimately repeat.
        """
        trade_id = (
            self._normalise_trade_id(
                trade_id
            )
        )

        if trade_id is None:
            return True

        seen = self._recent_trade_id_sets[
            symbol
        ]

        if trade_id in seen:
            return False

        queue = self._recent_trade_ids[
            symbol
        ]

        queue.append(
            trade_id
        )

        seen.add(
            trade_id
        )

        while (
            len(queue)
            > self._max_recent_trade_ids_per_symbol
        ):
            expired = queue.popleft()
            seen.discard(
                expired
            )

        return True


    def _record_duplicate_trade_locked(
        self,
        *,
        symbol: str,
        event_timestamp: float,
    ) -> None:
        for timeframe_seconds, bars_by_symbol in (
            self._live_bars.items()
        ):
            bars = bars_by_symbol.get(
                symbol
            )

            if not bars:
                continue

            bucket = self._bucket_start(
                event_timestamp,
                timeframe_seconds,
            )

            bar = self._find_live_bar_locked(
                bars,
                bucket,
            )

            if bar is not None:
                bar[
                    "duplicate_trade_message_count"
                ] = (
                    int(
                        bar.get(
                            "duplicate_trade_message_count",
                            0,
                        )
                        or 0
                    )
                    + 1
                )

    def _update_live_bar_locked(
        self,
        symbol: str,
        price: float,
        volume: float,
        event_timestamp: float,
        receipt_timestamp: float,
        timeframe_seconds: int,
        *,
        trade_id: Any = None,
        event_timestamp_source: str = (
            "trade.timestamp"
        ),
    ) -> dict:
        timeframe_seconds = int(
            timeframe_seconds
        )

        bars_by_symbol = (
            self._live_bars.setdefault(
                timeframe_seconds,
                {},
            )
        )

        if symbol not in bars_by_symbol:
            bars_by_symbol[symbol] = (
                SmartDeque(
                    maxlen=(
                        self._maxlen_live_bars
                    )
                )
            )

        bars = bars_by_symbol[symbol]

        bucket = self._bucket_start(
            event_timestamp,
            timeframe_seconds,
        )

        bucket_end = (
            bucket
            + timeframe_seconds
        )

        strategy_eligible_at = (
            bucket_end
            + self._live_bar_lateness_grace_seconds
        )

        bar = self._find_live_bar_locked(
            bars,
            bucket,
        )

        if bar is not None:
            if (
                not bool(
                    bar.get(
                        "sealed"
                    )
                )
                and receipt_timestamp
                >= float(
                    bar.get(
                        "strategy_eligible_at_epoch",
                        strategy_eligible_at,
                    )
                    or strategy_eligible_at
                )
            ):
                self._seal_bar_locked(
                    bar,
                    sealed_at_epoch=(
                        receipt_timestamp
                    ),
                )

            if bool(
                bar.get(
                    "sealed"
                )
            ):
                bar[
                    "late_after_seal_trade_count"
                ] = (
                    int(
                        bar.get(
                            "late_after_seal_trade_count",
                            0,
                        )
                        or 0
                    )
                    + 1
                )

                bar[
                    "late_after_seal_volume"
                ] = (
                    float(
                        bar.get(
                            "late_after_seal_volume",
                            0.0,
                        )
                        or 0.0
                    )
                    + max(
                        0.0,
                        volume,
                    )
                )

                bar[
                    "last_late_trade_event_timestamp"
                ] = datetime.fromtimestamp(
                    event_timestamp,
                    tz=timezone.utc,
                )

                bar[
                    "last_late_trade_received_timestamp"
                ] = datetime.fromtimestamp(
                    receipt_timestamp,
                    tz=timezone.utc,
                )

                return {
                    "updated": False,
                    "reason": (
                        "late_after_seal"
                    ),
                    "bucket_start": bucket,
                }

        if bar is None:
            existing_bars = list(
                bars or []
            )

            if existing_bars:
                newest_bucket = max(
                    int(
                        existing.get(
                            "bucket_start",
                            0,
                        )
                        or 0
                    )
                    for existing in (
                        existing_bars
                    )
                )

                # Never append an old missing bucket after newer bars.
                # If it is no longer retained, record it as dropped.
                if bucket < newest_bucket:
                    return {
                        "updated": False,
                        "reason": (
                            "late_bucket_not_retained"
                        ),
                        "bucket_start": bucket,
                    }

            trade_sort_key = (
                self._trade_sort_key(
                    event_timestamp=(
                        event_timestamp
                    ),
                    receipt_timestamp=(
                        receipt_timestamp
                    ),
                    trade_id=trade_id,
                )
            )

            latency_ms = (
                receipt_timestamp
                - event_timestamp
            ) * 1000.0

            new_volume = max(
                0.0,
                volume,
            )

            late_created = bool(
                receipt_timestamp
                >= strategy_eligible_at
            )

            bar = {
                "timestamp": self._bar_time(
                    bucket
                ),
                "bucket_start": bucket,
                "bucket_end": bucket_end,
                "timeframe_seconds": (
                    timeframe_seconds
                ),
                "strategy_eligible_at_epoch": (
                    strategy_eligible_at
                ),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": new_volume,
                "trade_count": 1.0,
                "price_volume_sum": (
                    price * new_volume
                ),
                "vwap": price,
                "first_trade_event_epoch": (
                    event_timestamp
                ),
                "last_trade_event_epoch": (
                    event_timestamp
                ),
                "first_trade_event_timestamp": (
                    datetime.fromtimestamp(
                        event_timestamp,
                        tz=timezone.utc,
                    )
                ),
                "last_trade_event_timestamp": (
                    datetime.fromtimestamp(
                        event_timestamp,
                        tz=timezone.utc,
                    )
                ),
                "first_trade_received_epoch": (
                    receipt_timestamp
                ),
                "last_trade_received_epoch": (
                    receipt_timestamp
                ),
                "first_trade_received_timestamp": (
                    datetime.fromtimestamp(
                        receipt_timestamp,
                        tz=timezone.utc,
                    )
                ),
                "last_trade_received_timestamp": (
                    datetime.fromtimestamp(
                        receipt_timestamp,
                        tz=timezone.utc,
                    )
                ),
                "min_receipt_latency_ms": (
                    latency_ms
                ),
                "max_receipt_latency_ms": (
                    latency_ms
                ),
                "last_receipt_latency_ms": (
                    latency_ms
                ),
                "event_timestamp_fallback_count": (
                    0
                    if event_timestamp_source
                    == "trade.timestamp"
                    else 1
                ),
                "trade_id_missing_count": (
                    0
                    if self._normalise_trade_id(
                        trade_id
                    )
                    is not None
                    else 1
                ),
                "duplicate_trade_message_count": 0,
                "late_after_seal_trade_count": 0,
                "late_after_seal_volume": 0.0,
                "late_created_after_seal": (
                    late_created
                ),
                "sealed": False,
                "sealed_at": None,
                "sealed_at_epoch": None,
                "capture_quality": "OPEN",
                "_first_trade_sort_key": (
                    trade_sort_key
                ),
                "_last_trade_sort_key": (
                    trade_sort_key
                ),
            }

            bars.append(
                bar
            )

            if late_created:
                self._seal_bar_locked(
                    bar,
                    sealed_at_epoch=(
                        receipt_timestamp
                    ),
                )

            return {
                "updated": True,
                "reason": "created",
                "bucket_start": bucket,
            }

        trade_sort_key = (
            self._trade_sort_key(
                event_timestamp=(
                    event_timestamp
                ),
                receipt_timestamp=(
                    receipt_timestamp
                ),
                trade_id=trade_id,
            )
        )

        first_sort_key = tuple(
            bar.get(
                "_first_trade_sort_key",
                trade_sort_key,
            )
        )

        last_sort_key = tuple(
            bar.get(
                "_last_trade_sort_key",
                trade_sort_key,
            )
        )

        if trade_sort_key < first_sort_key:
            bar["open"] = price

            bar[
                "_first_trade_sort_key"
            ] = trade_sort_key

            bar[
                "first_trade_event_epoch"
            ] = event_timestamp

            bar[
                "first_trade_event_timestamp"
            ] = datetime.fromtimestamp(
                event_timestamp,
                tz=timezone.utc,
            )

            bar[
                "first_trade_received_epoch"
            ] = receipt_timestamp

            bar[
                "first_trade_received_timestamp"
            ] = datetime.fromtimestamp(
                receipt_timestamp,
                tz=timezone.utc,
            )

        if trade_sort_key > last_sort_key:
            bar["close"] = price

            bar[
                "_last_trade_sort_key"
            ] = trade_sort_key

            bar[
                "last_trade_event_epoch"
            ] = event_timestamp

            bar[
                "last_trade_event_timestamp"
            ] = datetime.fromtimestamp(
                event_timestamp,
                tz=timezone.utc,
            )

            bar[
                "last_trade_received_epoch"
            ] = receipt_timestamp

            bar[
                "last_trade_received_timestamp"
            ] = datetime.fromtimestamp(
                receipt_timestamp,
                tz=timezone.utc,
            )

        bar["high"] = max(
            float(
                bar.get(
                    "high",
                    price,
                )
                or price
            ),
            price,
        )

        bar["low"] = min(
            float(
                bar.get(
                    "low",
                    price,
                )
                or price
            ),
            price,
        )

        old_volume = float(
            bar.get(
                "volume",
                0.0,
            )
            or 0.0
        )

        new_volume = max(
            0.0,
            volume,
        )

        combined_volume = (
            old_volume
            + new_volume
        )

        price_volume_sum = (
            float(
                bar.get(
                    "price_volume_sum",
                    0.0,
                )
                or 0.0
            )
            + (
                price
                * new_volume
            )
        )

        bar["volume"] = combined_volume

        bar["trade_count"] = (
            float(
                bar.get(
                    "trade_count",
                    0.0,
                )
                or 0.0
            )
            + 1.0
        )

        bar[
            "price_volume_sum"
        ] = price_volume_sum

        if combined_volume > 0:
            bar["vwap"] = (
                price_volume_sum
                / combined_volume
            )

        latency_ms = (
            receipt_timestamp
            - event_timestamp
        ) * 1000.0

        bar[
            "min_receipt_latency_ms"
        ] = min(
            float(
                bar.get(
                    "min_receipt_latency_ms",
                    latency_ms,
                )
                or latency_ms
            ),
            latency_ms,
        )

        bar[
            "max_receipt_latency_ms"
        ] = max(
            float(
                bar.get(
                    "max_receipt_latency_ms",
                    latency_ms,
                )
                or latency_ms
            ),
            latency_ms,
        )

        bar[
            "last_receipt_latency_ms"
        ] = latency_ms

        if (
            event_timestamp_source
            != "trade.timestamp"
        ):
            bar[
                "event_timestamp_fallback_count"
            ] = (
                int(
                    bar.get(
                        "event_timestamp_fallback_count",
                        0,
                    )
                    or 0
                )
                + 1
            )

        if (
            self._normalise_trade_id(
                trade_id
            )
            is None
        ):
            bar[
                "trade_id_missing_count"
            ] = (
                int(
                    bar.get(
                        "trade_id_missing_count",
                        0,
                    )
                    or 0
                )
                + 1
            )

        return {
            "updated": True,
            "reason": "updated",
            "bucket_start": bucket,
        }


    def update_tick(
        self,
        symbol: str,
        price: float,
        volume: float,
        timestamp: float,
        *,
        receipt_timestamp: float | None = None,
        trade_id: Any = None,
        event_timestamp_source: str = (
            "trade.timestamp"
        ),
    ) -> dict:
        """
        Add one streamed trade.

        timestamp is the market event timestamp.

        receipt_timestamp is when this process received the message and is
        retained only for latency and sealing diagnostics.
        """
        symbol = self._norm_symbol(
            symbol
        )

        if not symbol:
            return {
                "accepted": False,
                "reason": "missing_symbol",
            }

        event_timestamp = (
            self._to_epoch_seconds(
                timestamp
            )
        )

        if receipt_timestamp is None:
            receipt_timestamp = time.time()
        else:
            receipt_timestamp = (
                self._to_epoch_seconds(
                    receipt_timestamp
                )
            )

        price = float(
            price
        )

        volume = float(
            volume or 0.0
        )

        with self._lock:
            self._ensure_symbol(
                symbol
            )

            if not self._remember_trade_id_locked(
                symbol,
                trade_id,
            ):
                self._record_duplicate_trade_locked(
                    symbol=symbol,
                    event_timestamp=(
                        event_timestamp
                    ),
                )

                return {
                    "accepted": False,
                    "accepted_for_latest_price": (
                        False
                    ),
                    "reason": "duplicate_trade_id",
                }

            timeframe_results = {
                60: self._update_live_bar_locked(
                    symbol,
                    price,
                    volume,
                    event_timestamp,
                    receipt_timestamp,
                    60,
                    trade_id=trade_id,
                    event_timestamp_source=(
                        event_timestamp_source
                    ),
                ),
                300: self._update_live_bar_locked(
                    symbol,
                    price,
                    volume,
                    event_timestamp,
                    receipt_timestamp,
                    300,
                    trade_id=trade_id,
                    event_timestamp_source=(
                        event_timestamp_source
                    ),
                ),
            }

            recent_prices = self._prices[
                symbol
            ]

            last_event_timestamp = None

            if recent_prices:
                try:
                    last_event_timestamp = float(
                        list(
                            recent_prices
                        )[-1][0]
                    )
                except Exception:
                    last_event_timestamp = None

            accepted_for_latest_price = bool(
                last_event_timestamp is None
                or event_timestamp
                >= last_event_timestamp
            )

            if accepted_for_latest_price:
                self._prices[
                    symbol
                ].append((
                    event_timestamp,
                    price,
                ))

                self._volumes[
                    symbol
                ].append((
                    event_timestamp,
                    volume,
                ))

            else:
                self._out_of_order_tick_counts[
                    symbol
                ] = (
                    self._out_of_order_tick_counts.get(
                        symbol,
                        0,
                    )
                    + 1
                )

            return {
                "accepted": True,
                "accepted_for_latest_price": (
                    accepted_for_latest_price
                ),
                "reason": (
                    "accepted"
                    if accepted_for_latest_price
                    else
                    "accepted_bar_only_out_of_order"
                ),
                "event_timestamp": (
                    event_timestamp
                ),
                "receipt_timestamp": (
                    receipt_timestamp
                ),
                "timeframes": (
                    timeframe_results
                ),
            }


    def set_live_bar_lateness_grace_seconds(
        self,
        seconds: float,
    ) -> None:
        seconds = max(
            0.0,
            float(
                seconds or 0.0
            ),
        )

        with self._lock:
            self._live_bar_lateness_grace_seconds = (
                seconds
            )

            for timeframe_seconds, bars_by_symbol in (
                self._live_bars.items()
            ):
                for bars in (
                    bars_by_symbol.values()
                ):
                    for bar in bars or []:
                        if bool(
                            bar.get(
                                "sealed"
                            )
                        ):
                            continue

                        bucket_start = float(
                            bar.get(
                                "bucket_start",
                                0.0,
                            )
                            or 0.0
                        )

                        bar[
                            "strategy_eligible_at_epoch"
                        ] = (
                            bucket_start
                            + timeframe_seconds
                            + seconds
                        )


    def mark_stream_connected(
        self,
        timestamp: Any = None,
    ) -> None:
        timestamp = self._to_epoch_seconds(
            timestamp
        )

        with self._lock:
            if (
                self._stream_connection_intervals
                and self._stream_connection_intervals[
                    -1
                ].get(
                    "ended_at_epoch"
                )
                is None
            ):
                return

            self._stream_connection_intervals.append({
                "started_at_epoch": (
                    timestamp
                ),
                "ended_at_epoch": None,
            })

            if (
                len(
                    self._stream_connection_intervals
                )
                > self._max_stream_connection_intervals
            ):
                self._stream_connection_intervals = (
                    self._stream_connection_intervals[
                        -self._max_stream_connection_intervals:
                    ]
                )


    def mark_stream_disconnected(
        self,
        timestamp: Any = None,
    ) -> None:
        timestamp = self._to_epoch_seconds(
            timestamp
        )

        with self._lock:
            if not self._stream_connection_intervals:
                return

            latest = (
                self._stream_connection_intervals[
                    -1
                ]
            )

            if latest.get(
                "ended_at_epoch"
            ) is None:
                latest[
                    "ended_at_epoch"
                ] = timestamp


    def get_live_bar_construction_status(
        self,
    ) -> dict:
        with self._lock:
            return {
                "lateness_grace_seconds": (
                    self._live_bar_lateness_grace_seconds
                ),
                "connection_intervals": [
                    dict(
                        interval
                    )
                    for interval in (
                        self._stream_connection_intervals
                    )
                ],
                "out_of_order_tick_counts": dict(
                    self._out_of_order_tick_counts
                ),
            }


    # ─────────────────────────────────────────────
    # ✅ Prices-only / volumes-only (preferred for strategies)
    # ─────────────────────────────────────────────
    def get_recent_prices(self, symbol: str, limit: int | None = None) -> List[float]:
        symbol = self._norm_symbol(symbol)
        with self._lock:
            dq = self._prices.get(symbol)
            if not dq:
                return []
            data = [p for (_t, p) in dq]
            return data[-limit:] if (limit and limit > 0) else data

    def get_recent_volumes(self, symbol: str, limit: int | None = None) -> List[float]:
        symbol = self._norm_symbol(symbol)
        with self._lock:
            dq = self._volumes.get(symbol)
            if not dq:
                return []
            data = [v for (_t, v) in dq]
            return data[-limit:] if (limit and limit > 0) else data

    # ─────────────────────────────────────────────
    # ✅ Timestamped getters (for Δt logic like VolatilityScorer)
    # ─────────────────────────────────────────────
    def get_recent_prices_ts(self, symbol: str, limit: int | None = None) -> List[Tuple[float, float]]:
        symbol = self._norm_symbol(symbol)
        with self._lock:
            dq = self._prices.get(symbol)
            data = list(dq) if dq else []
            return data[-limit:] if (limit and limit > 0) else data


    def get_recent_volumes_ts(self, symbol: str, limit: int | None = None) -> List[Tuple[float, float]]:
        symbol = self._norm_symbol(symbol)
        with self._lock:
            dq = self._volumes.get(symbol)
            data = list(dq) if dq else []
            return data[-limit:] if (limit and limit > 0) else data


    def get_last_price(self, symbol: str) -> Optional[float]:
        prices = self.get_recent_prices(symbol)
        return prices[-1] if prices else None


    def get_live_bars(
        self,
        symbol: str,
        timeframe_seconds: int = 60,
        limit: int | None = None,
        completed_only: bool = False,
    ) -> List[dict]:
        """
        Return rolling event-time bars built from live IEX trade messages.

        completed_only returns bars that have passed the lateness grace period
        and have been sealed against further OHLCV mutation.
        """
        symbol = self._norm_symbol(
            symbol
        )

        timeframe_seconds = int(
            timeframe_seconds
        )

        with self._lock:
            self._seal_mature_bars_locked(
                time.time()
            )

            bars_by_symbol = (
                self._live_bars.get(
                    timeframe_seconds,
                    {},
                )
            )

            dq = bars_by_symbol.get(
                symbol
            )

            data = [
                dict(
                    bar
                )
                for bar in dq
            ] if dq else []

        if completed_only:
            data = [
                bar
                for bar in data
                if bool(
                    bar.get(
                        "sealed"
                    )
                )
            ]

        return (
            data[-limit:]
            if (
                limit
                and limit > 0
            )
            else data
        )


    def get_live_bar_snapshot(self, symbol: str) -> dict:
        return {
            "1m": self.get_live_bars(symbol, timeframe_seconds=60, limit=10),
            "5m": self.get_live_bars(symbol, timeframe_seconds=300, limit=10),
        }


    def set_maxlen(self, prices_maxlen: Optional[int] = None, volumes_maxlen: Optional[int] = None) -> None:
        with self._lock:
            if prices_maxlen is not None:
                self._maxlen_prices = int(prices_maxlen)
                for dq in self._prices.values():
                    dq.set_maxlen(self._maxlen_prices)

            if volumes_maxlen is not None:
                self._maxlen_volumes = int(volumes_maxlen)
                for dq in self._volumes.values():
                    dq.set_maxlen(self._maxlen_volumes)


    def snapshot(self) -> dict:
        with self._lock:
            out = {}

            for sym, dq in self._prices.items():
                tail = list(dq)[-5:]

                bars_1m = self._live_bars.get(60, {}).get(sym)
                bars_5m = self._live_bars.get(300, {}).get(sym)

                out[sym] = {
                    "prices_tail_ts": tail,
                    "tick_count": len(dq),
                    "live_1m_bar_count": len(bars_1m) if bars_1m else 0,
                    "live_5m_bar_count": len(bars_5m) if bars_5m else 0,
                    "live_1m_tail": list(bars_1m)[-3:] if bars_1m else [],
                    "live_5m_tail": list(bars_5m)[-3:] if bars_5m else [],
                }

            return out