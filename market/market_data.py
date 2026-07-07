# market/market_data.py
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple, Optional

from core.state import SmartDeque


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


    def __init__(self, maxlen_prices: int = 25, maxlen_volumes: int = 25):
        self._lock = threading.Lock()

        # Internal storage ALWAYS keeps timestamps
        self._prices: Dict[str, SmartDeque] = {}   # SmartDeque of (t, price)
        self._volumes: Dict[str, SmartDeque] = {}  # SmartDeque of (t, volume)

        # Rolling live bars built directly from trade ticks.
        # These are intentionally for tactical/diagnostic use first; the main
        # Layer 1/2 ranking engine still consumes Alpaca REST bars.
        self._live_bars: Dict[int, Dict[str, SmartDeque]] = {
            60: {},
            300: {},
        }
        self._maxlen_live_bars = 500

        self._maxlen_prices = int(maxlen_prices)
        self._maxlen_volumes = int(maxlen_volumes)


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


    def _update_live_bar_locked(
        self,
        symbol: str,
        price: float,
        volume: float,
        timestamp: float,
        timeframe_seconds: int,
    ) -> None:
        bars_by_symbol = self._live_bars.setdefault(int(timeframe_seconds), {})

        if symbol not in bars_by_symbol:
            bars_by_symbol[symbol] = SmartDeque(maxlen=self._maxlen_live_bars)

        bars = bars_by_symbol[symbol]
        bucket = self._bucket_start(timestamp, timeframe_seconds)

        if not bars or int(bars[-1]["bucket_start"]) != bucket:
            bars.append({
                "timestamp": self._bar_time(bucket),
                "bucket_start": bucket,
                "timeframe_seconds": int(timeframe_seconds),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": max(0.0, volume),
                "trade_count": 1.0,
                "vwap": price,
            })
            return

        bar = bars[-1]

        old_volume = float(bar.get("volume", 0.0) or 0.0)
        new_volume = max(0.0, volume)
        combined_volume = old_volume + new_volume

        bar["high"] = max(float(bar.get("high", price) or price), price)
        bar["low"] = min(float(bar.get("low", price) or price), price)
        bar["close"] = price
        bar["volume"] = combined_volume
        bar["trade_count"] = float(bar.get("trade_count", 0.0) or 0.0) + 1.0

        if combined_volume > 0 and new_volume > 0:
            old_vwap = float(bar.get("vwap", price) or price)
            bar["vwap"] = ((old_vwap * old_volume) + (price * new_volume)) / combined_volume
        else:
            bar["vwap"] = price


    def update_tick(
        self,
        symbol: str,
        price: float,
        volume: float,
        timestamp: float,
    ) -> None:
        symbol = self._norm_symbol(symbol)
        if not symbol:
            return

        timestamp = self._to_epoch_seconds(timestamp)
        price = float(price)
        volume = float(volume or 0.0)

        with self._lock:
            self._ensure_symbol(symbol)
            self._prices[symbol].append((timestamp, price))
            self._volumes[symbol].append((timestamp, volume))
            self._update_live_bar_locked(symbol, price, volume, timestamp, 60)
            self._update_live_bar_locked(symbol, price, volume, timestamp, 300)


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
        Return rolling bars built from live trade ticks.

        These bars are based on the live IEX stream available to the bot, not
        consolidated SIP bars. Use them for Layer 4 timing/pressure diagnostics
        before letting them influence Layer 1/2/3.
        """
        symbol = self._norm_symbol(symbol)
        timeframe_seconds = int(timeframe_seconds)

        with self._lock:
            bars_by_symbol = self._live_bars.get(timeframe_seconds, {})
            dq = bars_by_symbol.get(symbol)
            data = [dict(bar) for bar in dq] if dq else []

        if completed_only and data:
            now_ts = time.time()
            data = [
                bar
                for bar in data
                if float(bar.get("bucket_start", 0) or 0) + timeframe_seconds <= now_ts
            ]

        return data[-limit:] if (limit and limit > 0) else data


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