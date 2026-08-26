from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import hashlib
import io
import itertools
import json
import math
import os
import platform
import statistics
import sys
import time
import uuid
import zipfile
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from layers.layer1_ranker import Layer1StockRanker
from layers.layer2_portfolio import Layer2PortfolioBuilder
from layers.layer3_rebalancer import build_layer3_shadow_plan, source_bar_market_session_info
from layers.layer_research_strategy import STRATEGIES, _raw_research_target, _smooth_target
from research.walk_forward import build_walk_forward_folds
from research.universes import SECTORS, resolve_universe, universe_metadata
from utils.numeric import safe_float


UTC = timezone.utc
MARKET_TZ = ZoneInfo("America/New_York")
REQUIRED_COLUMNS = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}
CSV_ARTIFACTS = {
    "cycles": "replay_cycles.csv",
    "daily": "replay_daily.csv",
    "decisions": "replay_decisions.csv",
    "orders": "replay_orders.csv",
    "dataset": "ml_dataset.csv",
    "eligibility": "replay_eligibility.csv",
    "benchmark_daily": "benchmark_daily.csv",
    "walk_forward_results": "walk_forward_results.csv",
    "account_profiles": "account_profile_results.csv",
    "cross_account_wash_sales": "cross_account_wash_sale_matrix.csv",
    "universe_selection": "universe_selection_diagnostics.csv",
}


@dataclass(frozen=True, slots=True)
class BarRow(Mapping):
    """Compact immutable OHLCV row used for large hosted replays."""

    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: float
    vwap: float

    _fields = (
        "timestamp", "symbol", "open", "high", "low", "close", "volume",
        "trade_count", "vwap",
    )

    def __getitem__(self, key):
        if key not in self._fields:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self):
        return iter(self._fields)

    def __len__(self):
        return len(self._fields)


@dataclass(frozen=True, slots=True)
class LabelBar(Mapping):
    """Reduced bar retained after replay for forward-label construction."""

    timestamp: datetime
    high: float
    low: float
    close: float

    _fields = ("timestamp", "high", "low", "close")

    def __getitem__(self, key):
        if key not in self._fields:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self):
        return iter(self._fields)

    def __len__(self):
        return len(self._fields)


class SpilledRows:
    """Append-only JSONL collection used to keep large diagnostics off the heap."""

    def __init__(
        self, root: str | Path, name: str, *, existing_path: str | Path | None = None,
        existing_count: int = 0,
    ) -> None:
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.path = Path(existing_path) if existing_path else root / f"{name}-{uuid.uuid4().hex}.jsonl.gz"
        self._count = int(existing_count)
        self._handle = gzip.open(
            self.path, "at", encoding="utf-8", compresslevel=1
        )

    def append(self, row: dict) -> None:
        self._handle.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
        self._count += 1

    def extend(self, rows) -> None:
        for row in rows:
            self._handle.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
            self._count += 1

    def __iter__(self):
        if not self._handle.closed:
            self._handle.close()
        if not self.path.exists():
            return
        with gzip.open(self.path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)

    def __len__(self) -> int:
        return self._count

    def close(self) -> None:
        if hasattr(self, "_handle") and not self._handle.closed:
            self._handle.close()

    def checkpoint(self) -> dict:
        """Close the gzip member so its bytes form a crash-safe snapshot."""
        self.close()
        return {"path": str(self.path), "count": self._count}

    def reopen(self) -> None:
        if self._handle.closed:
            self._handle = gzip.open(
                self.path, "at", encoding="utf-8", compresslevel=1
            )

    def __del__(self):
        self.close()


@dataclass(frozen=True)
class ReplayConfig:
    initial_cash: float = 100000.0
    warmup_bars: int = 61
    decision_interval_minutes: int = 5
    spread_bps: float = 1.0
    slippage_bps: float = 1.0
    commission_per_order: float = 0.0
    plan_ttl_minutes: int = 10
    min_train_sessions: int = 60
    test_sessions: int = 20
    step_sessions: int = 20
    benchmark_symbol: str = "SPY"
    candidate_symbols: tuple[str, ...] = ()
    universe_name: str = ""
    require_benchmark: bool = True
    minimum_average_coverage_pct: float = 95.0
    maximum_candidate_bar_age_minutes: float | None = 10.0
    minimum_eligible_symbols: int = 8
    minimum_eligible_coverage_pct: float = 80.0
    bar_timestamp_semantics: str = "bar_start"
    data_start_date: str | None = None
    data_end_date: str | None = None
    taxable_short_term_rate: float = 0.22
    taxable_long_term_rate: float = 0.15
    taxable_state_rate: float = 0.093
    taxpayer_filing_status: str = "single"
    taxpayer_state: str = "CA"
    taxpayer_gross_income: float = 105000.0

    def __post_init__(self) -> None:
        start = (
            datetime.fromisoformat(self.data_start_date).date()
            if self.data_start_date else None
        )
        end = (
            datetime.fromisoformat(self.data_end_date).date()
            if self.data_end_date else None
        )
        if start and end and start > end:
            raise ValueError("data_start_date must be on or before data_end_date")
        if self.maximum_candidate_bar_age_minutes is not None and self.maximum_candidate_bar_age_minutes < 0:
            raise ValueError("maximum_candidate_bar_age_minutes cannot be negative")
        if self.minimum_eligible_symbols < 1:
            raise ValueError("minimum_eligible_symbols must be positive")
        if not 0.0 < self.minimum_eligible_coverage_pct <= 100.0:
            raise ValueError("minimum_eligible_coverage_pct must be above zero and at most 100")
        if self.universe_name:
            resolve_universe(self.universe_name)
        if self.universe_name and self.candidate_symbols:
            raise ValueError("set universe_name or candidate_symbols, not both")
        for name in ("taxable_short_term_rate", "taxable_long_term_rate", "taxable_state_rate"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between zero and one")
        if self.taxpayer_gross_income < 0:
            raise ValueError("taxpayer_gross_income cannot be negative")

    @property
    def adverse_fill_bps(self) -> float:
        return max(0.0, self.spread_bps / 2.0 + self.slippage_bps)


def _checkpoint_interval(completion_ratio: float, base_interval: int) -> int:
    base_interval = max(1, int(base_interval))
    if completion_ratio >= 0.90:
        return 1
    if completion_ratio >= 0.75:
        return min(5, base_interval)
    return base_interval


@dataclass
class ReplayPortfolio:
    cash: float
    positions: dict[str, float] = field(default_factory=dict)
    planner_state: dict = field(default_factory=dict)
    previous_target: dict | None = None
    pending_plan: list[dict] = field(default_factory=list)
    pending_at: datetime | None = None
    peak_equity: float = 0.0
    turnover: float = 0.0
    trade_count: int = 0
    reversals: int = 0
    last_side: dict[str, str] = field(default_factory=dict)
    tax_lots: dict[str, list[dict]] = field(default_factory=dict)
    pending_wash_losses: dict[str, list[dict]] = field(default_factory=dict)
    realized_short_term_gain: float = 0.0
    realized_long_term_gain: float = 0.0
    realized_loss: float = 0.0
    wash_sale_loss_exposure: float = 0.0
    buy_events: list[dict] = field(default_factory=list)
    loss_sale_events: list[dict] = field(default_factory=list)


def _record_tax_fill(
    portfolio: ReplayPortfolio, symbol: str, side: str, qty: float,
    fill_price: float, ts: datetime,
) -> None:
    """Maintain an estimated FIFO tax ledger for account-profile research."""
    lots = portfolio.tax_lots.setdefault(symbol, [])
    pending = portfolio.pending_wash_losses.setdefault(symbol, [])
    cutoff = ts - timedelta(days=30)
    pending[:] = [item for item in pending if _timestamp(item["sold_at"]) >= cutoff]
    if side == "BUY":
        _append_daily_tax_event(
            portfolio.buy_events,
            {"symbol": symbol, "bought_at": ts.isoformat(), "qty": qty},
            timestamp_key="bought_at",
        )
        remaining = qty
        basis_adjustment = 0.0
        for loss in pending:
            matched = min(remaining, float(loss["remaining_qty"]))
            if matched <= 0:
                continue
            basis_adjustment += matched * float(loss["loss_per_share"])
            loss["remaining_qty"] -= matched
            remaining -= matched
            portfolio.wash_sale_loss_exposure += matched * float(loss["loss_per_share"])
        pending[:] = [item for item in pending if float(item["remaining_qty"]) > 0]
        adjusted_cost = fill_price + basis_adjustment / qty
        if lots and _timestamp(lots[-1]["acquired_at"]).date() == ts.date():
            prior_qty = float(lots[-1]["qty"])
            combined_qty = prior_qty + qty
            lots[-1]["cost_per_share"] = (
                prior_qty * float(lots[-1]["cost_per_share"])
                + qty * adjusted_cost
            ) / combined_qty
            lots[-1]["qty"] = combined_qty
        else:
            lots.append({
                "qty": qty, "cost_per_share": adjusted_cost,
                "acquired_at": ts.isoformat(),
            })
        return
    remaining = qty
    while remaining > 0 and lots:
        lot = lots[0]
        sold = min(remaining, float(lot["qty"]))
        gain = sold * (fill_price - float(lot["cost_per_share"]))
        held_days = (ts - _timestamp(lot["acquired_at"])).days
        if gain >= 0:
            if held_days > 365:
                portfolio.realized_long_term_gain += gain
            else:
                portfolio.realized_short_term_gain += gain
        else:
            loss = -gain
            portfolio.realized_loss += loss
            pending.append({
                "sold_at": ts.isoformat(), "remaining_qty": sold,
                "loss_per_share": loss / sold,
            })
            _append_daily_tax_event(
                portfolio.loss_sale_events,
                {
                    "symbol": symbol, "sold_at": ts.isoformat(), "qty": sold,
                    "loss_per_share": loss / sold,
                },
                timestamp_key="sold_at", weighted_key="loss_per_share",
            )
        lot["qty"] -= sold
        remaining -= sold
        if lot["qty"] <= 0:
            lots.pop(0)


def _append_daily_tax_event(
    events: list[dict], event: dict, *, timestamp_key: str,
    weighted_key: str | None = None,
) -> None:
    """Compact same-symbol intraday tax events without changing day-level results.

    Cross-account wash-sale diagnostics only use the symbol, calendar day,
    quantity, and (for losses) total loss. Keeping every five-minute fill made
    long replay checkpoints grow without bound and caused large serialization
    spikes on memory-constrained workers.
    """
    event_day = _timestamp(event[timestamp_key]).date()
    existing = next((
        item for item in reversed(events)
        if item["symbol"] == event["symbol"]
        and _timestamp(item[timestamp_key]).date() == event_day
    ), None)
    if existing is None:
        events.append(event)
        return
    old_qty = float(existing["qty"])
    added_qty = float(event["qty"])
    total_qty = old_qty + added_qty
    if weighted_key and total_qty:
        existing[weighted_key] = (
            old_qty * float(existing[weighted_key])
            + added_qty * float(event[weighted_key])
        ) / total_qty
    existing["qty"] = total_qty


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def load_bar_csv(
    path: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    include_symbols: set[str] | None = None,
) -> list[dict]:
    """Load long-form OHLCV bars and reject ambiguous historical input."""
    path = Path(path)
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"bar CSV missing required columns: {sorted(missing)}")
        rows = []
        timestamp_cache = {}
        for line, raw in enumerate(reader, start=2):
            source_timestamp = datetime.fromisoformat(
                str(raw["timestamp"]).replace("Z", "+00:00")
            )
            if source_timestamp.tzinfo is None:
                raise ValueError(f"timestamp missing UTC offset at line {line}")
            session_date = source_timestamp.astimezone(
                MARKET_TZ
            ).date().isoformat()
            if start_date and session_date < start_date:
                continue
            if end_date and session_date > end_date:
                continue
            timestamp_text = str(raw["timestamp"])
            ts = timestamp_cache.get(timestamp_text)
            if ts is None:
                ts = source_timestamp.astimezone(UTC)
                timestamp_cache[timestamp_text] = ts
            symbol = sys.intern(str(raw["symbol"] or "").upper().strip())
            if not symbol:
                raise ValueError(f"invalid bar at line {line}: missing symbol")
            if include_symbols and symbol not in include_symbols:
                continue
            values = {}
            for name in ("open", "high", "low", "close", "volume", "trade_count", "vwap"):
                value = raw.get(name)
                values[name] = float(value) if value not in (None, "") else 0.0
            row = BarRow(timestamp=ts, symbol=symbol, **values)
            if min(row["open"], row["high"], row["low"], row["close"]) <= 0:
                raise ValueError(f"non-positive OHLC value at line {line}")
            if row["high"] < max(row["open"], row["close"], row["low"]):
                raise ValueError(f"invalid high at line {line}")
            if row["low"] > min(row["open"], row["close"], row["high"]):
                raise ValueError(f"invalid low at line {line}")
            rows.append(row)
    rows.sort(key=lambda row: (row["timestamp"], row["symbol"]))
    for previous, current in zip(rows, rows[1:]):
        previous_key = (previous["timestamp"], previous["symbol"])
        current_key = (current["timestamp"], current["symbol"])
        if previous_key == current_key:
            raise ValueError(f"invalid or duplicate bar: {current_key}")
    return rows


def _is_regular_session(ts: datetime) -> bool:
    local = ts.astimezone(MARKET_TZ)
    minute = local.hour * 60 + local.minute
    return local.weekday() < 5 and 570 <= minute < 960


def _returns(closes: list[float]) -> dict[str, float]:
    def ret(bars: int) -> float:
        return closes[-1] / closes[-bars - 1] - 1.0 if len(closes) > bars else 0.0
    return {
        "ret_5m": ret(1), "ret_30m": ret(6), "ret_60m": ret(12),
        "ret_150m": ret(30), "ret_300m": ret(60),
    }


def _market_features(bars_by_symbol: dict[str, list[dict]]) -> dict:
    rows = []
    for bars in bars_by_symbol.values():
        closes = [float(item["close"]) for item in bars]
        rows.append(_returns(closes))
    output = {}
    for horizon in ("30m", "60m", "150m", "300m"):
        values = [row[f"ret_{horizon}"] for row in rows]
        output[f"market_mean_ret_{horizon}"] = sum(values) / len(values) if values else 0.0
        output[f"market_positive_breadth_{horizon}"] = (
            sum(value > 0 for value in values) / len(values) if values else 0.0
        )
        output[f"market_dispersion_{horizon}"] = statistics.pstdev(values) if len(values) > 1 else 0.0
    return output


def _equity(portfolio: ReplayPortfolio, prices: dict[str, float]) -> float:
    return portfolio.cash + sum(
        qty * prices.get(symbol, 0.0) for symbol, qty in portfolio.positions.items()
    )


def _position_snapshot(portfolio: ReplayPortfolio, prices: dict[str, float]) -> dict:
    return {
        symbol: {
            "symbol": symbol, "qty": qty, "current_price": prices.get(symbol, 0.0),
            "market_value": qty * prices.get(symbol, 0.0),
            "avg_entry_price": prices.get(symbol, 0.0), "unrealized_plpc": 0.0,
        }
        for symbol, qty in portfolio.positions.items() if qty > 0
    }


def _execute_pending(
    name: str, portfolio: ReplayPortfolio, ts: datetime, bars: dict[str, dict],
    config: ReplayConfig,
) -> list[dict]:
    if not portfolio.pending_plan or portfolio.pending_at is None:
        return []
    age = (ts - portfolio.pending_at).total_seconds()
    if age <= 0:
        return []
    if age > config.plan_ttl_minutes * 60:
        portfolio.pending_plan = []
        portfolio.pending_at = None
        return []

    rows = []
    adverse = config.adverse_fill_bps / 10000.0
    plan = sorted(
        portfolio.pending_plan,
        key=lambda row: 0 if str(row.get("decision")).upper() == "SELL" else 1,
    )
    for item in plan:
        side = str(item.get("decision") or "").upper()
        symbol = str(item.get("symbol") or "").upper()
        if side not in {"BUY", "SELL"} or symbol not in bars:
            continue
        requested = math.floor(float(item.get("planned_qty") or 0.0))
        raw_price = float(bars[symbol]["open"])
        fill_price = raw_price * (1.0 + adverse if side == "BUY" else 1.0 - adverse)
        if side == "SELL":
            qty = min(requested, math.floor(portfolio.positions.get(symbol, 0.0)))
        else:
            affordable = math.floor(max(0.0, portfolio.cash - config.commission_per_order) / fill_price)
            qty = min(requested, affordable)
        if qty <= 0:
            continue
        notional = qty * fill_price
        if side == "BUY":
            portfolio.cash -= notional + config.commission_per_order
            portfolio.positions[symbol] = portfolio.positions.get(symbol, 0.0) + qty
        else:
            portfolio.cash += notional - config.commission_per_order
            portfolio.positions[symbol] = portfolio.positions.get(symbol, 0.0) - qty
            if portfolio.positions[symbol] <= 0:
                portfolio.positions.pop(symbol, None)
        _record_tax_fill(portfolio, symbol, side, qty, fill_price, ts)
        previous = portfolio.last_side.get(symbol)
        if previous and previous != side:
            portfolio.reversals += 1
        portfolio.last_side[symbol] = side
        portfolio.turnover += notional
        portfolio.trade_count += 1
        rows.append({
            "timestamp": ts.isoformat(), "strategy_name": name, "symbol": symbol,
            "side": side.lower(), "qty": qty, "decision_price": item.get("live_price"),
            "raw_next_open": round(raw_price, 6), "fill_price": round(fill_price, 6),
            "notional": round(notional, 2), "cost_bps": config.adverse_fill_bps,
            "commission": config.commission_per_order,
            "source_bar_timestamp": portfolio.pending_at.isoformat(),
            "decision_available_at": (portfolio.pending_at + timedelta(minutes=5)).isoformat(),
        })
    portfolio.pending_plan = []
    portfolio.pending_at = None
    return rows


def _future_labels(
    features, bars_by_symbol: dict, output=None, progress_callback=None,
    yield_every: int = 50,
):
    output = output if output is not None else []
    horizons = {"10m": 10, "30m": 30, "60m": 60}
    series = {}
    for symbol, values in bars_by_symbol.items():
        values = list(values)
        values.sort(key=lambda item: item[0] if isinstance(item, tuple) else item["timestamp"])
        timestamps = [
            item[0] if isinstance(item, tuple) else item["timestamp"]
            for item in values
        ]
        series[symbol] = (
            values, timestamps,
        )
    total_features = len(features)
    for feature_index, feature in enumerate(features, start=1):
        ts, symbol = _timestamp(feature["timestamp"]), feature["symbol"]
        values, timestamps = series[symbol]
        next_index = bisect.bisect_right(timestamps, ts)
        row = dict(feature)
        # A sparse feed can legitimately have no bar for this symbol at the
        # cohort timestamp. The feature close is the exact stale-safe value
        # available to the strategy at that decision point.
        start = float(feature["close"])
        for label, minutes in horizons.items():
            target_ts = ts + timedelta(minutes=minutes)
            target_index = bisect.bisect_left(timestamps, target_ts)
            target_item = values[target_index] if target_index < len(values) else None
            target_bar = (
                target_item[1] if isinstance(target_item, tuple) else target_item
            ) if target_item is not None and timestamps[target_index] == target_ts else None
            valid = target_bar is not None and target_ts.astimezone(MARKET_TZ).date() == ts.astimezone(MARKET_TZ).date()
            row[f"forward_return_{label}"] = (
                round(float(target_bar["close"]) / start - 1.0, 10) if valid else None
            )
        window_end = ts + timedelta(minutes=60)
        window = []
        for future_item in itertools.islice(values, next_index, None):
            future_ts = future_item[0] if isinstance(future_item, tuple) else future_item["timestamp"]
            future_bar = future_item[1] if isinstance(future_item, tuple) else future_item
            if future_ts > window_end:
                break
            if future_ts.astimezone(MARKET_TZ).date() == ts.astimezone(MARKET_TZ).date():
                window.append(future_bar)
        row["max_favorable_excursion_60m"] = (
            round(max(float(item["high"]) for item in window) / start - 1.0, 10) if window else None
        )
        row["max_adverse_excursion_60m"] = (
            round(min(float(item["low"]) for item in window) / start - 1.0, 10) if window else None
        )
        row["label_available_60m"] = row["forward_return_60m"] is not None
        output.append(row)
        if yield_every > 0 and feature_index % yield_every == 0:
            time.sleep(0.01)
        if progress_callback and (
            feature_index == total_features or feature_index % 1000 == 0
        ):
            progress_callback({
                "stage": "building_future_labels",
                "stage_completed_rows": feature_index,
                "stage_total_rows": total_features,
                "stage_percent_complete": round(
                    feature_index / total_features * 100, 2
                ) if total_features else 100.0,
                "percent_complete": 100.0,
            })
    return output


def _daily_summaries(cycles, orders, prior_close_equity: dict | None = None) -> list[dict]:
    grouped = {}
    order_grouped = defaultdict(lambda: {"trade_count": 0, "gross_turnover": 0.0})
    for row in cycles:
        key = (row["session_date"], row["strategy_name"])
        aggregate = grouped.setdefault(key, {
            "first": row, "last": row,
            "maximum_drawdown_pct": float(row["drawdown_pct"]),
            "peak_giveback": float(row["peak_giveback"]),
        })
        aggregate["last"] = row
        aggregate["maximum_drawdown_pct"] = min(
            aggregate["maximum_drawdown_pct"], float(row["drawdown_pct"])
        )
        aggregate["peak_giveback"] = min(
            aggregate["peak_giveback"], float(row["peak_giveback"])
        )
    for row in orders:
        session_date = _timestamp(row["timestamp"]).astimezone(MARKET_TZ).date().isoformat()
        aggregate = order_grouped[(session_date, row["strategy_name"])]
        aggregate["trade_count"] += 1
        aggregate["gross_turnover"] += float(row["notional"])
    output = []
    previous = dict(prior_close_equity or {})
    for (session_date, strategy), aggregate in sorted(grouped.items()):
        trades = order_grouped[(session_date, strategy)]
        start, end = aggregate["first"], aggregate["last"]
        prior_equity = previous.get(strategy, start["equity"])
        overnight_pnl = start["equity"] - prior_equity
        intraday_pnl = end["equity"] - start["equity"]
        output.append({
            "session_date": session_date, "strategy_name": strategy,
            "first_equity": start["equity"], "last_equity": end["equity"],
            "overnight_pnl": round(overnight_pnl, 2),
            "overnight_return": round(start["equity"] / prior_equity - 1.0, 10) if prior_equity else None,
            "intraday_pnl": round(intraday_pnl, 2),
            "intraday_return": round(end["equity"] / start["equity"] - 1.0, 10) if start["equity"] else None,
            "session_pnl": round(end["equity"] - prior_equity, 2),
            "session_return": round(end["equity"] / prior_equity - 1.0, 10) if prior_equity else None,
            "maximum_drawdown_pct": aggregate["maximum_drawdown_pct"],
            "peak_giveback": aggregate["peak_giveback"],
            "trade_count": trades["trade_count"],
            "gross_turnover": round(trades["gross_turnover"], 2),
            "ending_cash_pct": end["cash_pct"],
            "ending_reversal_exposure_pct": end["reversal_exposure_pct"],
        })
        previous[strategy] = end["equity"]
    return output


def _account_profile_summaries(
    portfolios: dict[str, ReplayPortfolio], summaries: list[dict],
    ending_prices: dict[str, float], ending_at: datetime, config: ReplayConfig,
) -> list[dict]:
    """Estimate comparable pretax, taxable, and Roth terminal outcomes.

    Taxable values use FIFO lots and a hypothetical terminal liquidation. They
    are research estimates, not tax-return calculations or tax advice.
    """
    summary_by_name = {row["strategy_name"]: row for row in summaries}
    output = []
    for name, portfolio in portfolios.items():
        summary = summary_by_name[name]
        final_equity = float(summary["final_equity"])
        unrealized_short = 0.0
        unrealized_long = 0.0
        for symbol, lots in portfolio.tax_lots.items():
            price = float(ending_prices.get(symbol, 0.0))
            for lot in lots:
                gain = float(lot["qty"]) * (price - float(lot["cost_per_share"]))
                acquired = _timestamp(lot["acquired_at"])
                if (ending_at - acquired).days > 365:
                    unrealized_long += gain
                else:
                    unrealized_short += gain
        total_short = portfolio.realized_short_term_gain + unrealized_short
        total_long = portfolio.realized_long_term_gain + unrealized_long
        deductible_losses = portfolio.realized_loss
        net_short = total_short - min(max(total_short, 0.0), deductible_losses)
        deductible_losses -= min(max(total_short, 0.0), deductible_losses)
        net_long = total_long - min(max(total_long, 0.0), deductible_losses)
        federal_tax = (
            max(0.0, net_short) * config.taxable_short_term_rate
            + max(0.0, net_long) * config.taxable_long_term_rate
        )
        state_tax = max(0.0, net_short + net_long) * config.taxable_state_rate
        taxable_liability = federal_tax + state_tax
        common = {
            "strategy_name": name,
            "pretax_final_equity": round(final_equity, 2),
            "pretax_return": round(final_equity / config.initial_cash - 1.0, 10),
            "turnover": summary["turnover"], "trade_count": summary["trade_count"],
            "max_drawdown_pct": summary["max_drawdown_pct"],
        }
        output.extend((
            {
                **common, "account_profile": "PRETAX",
                "estimated_tax_liability": 0.0,
                "estimated_after_tax_equity": round(final_equity, 2),
                "estimated_after_tax_return": common["pretax_return"],
            },
            {
                **common, "account_profile": "CA_SINGLE_105K",
                "estimated_tax_liability": round(taxable_liability, 2),
                "estimated_after_tax_equity": round(final_equity - taxable_liability, 2),
                "estimated_after_tax_return": round(
                    (final_equity - taxable_liability) / config.initial_cash - 1.0, 10
                ),
                "realized_short_term_gain": round(portfolio.realized_short_term_gain, 2),
                "realized_long_term_gain": round(portfolio.realized_long_term_gain, 2),
                "realized_loss": round(portfolio.realized_loss, 2),
                "unrealized_short_term_gain": round(unrealized_short, 2),
                "unrealized_long_term_gain": round(unrealized_long, 2),
                "wash_sale_loss_exposure": round(portfolio.wash_sale_loss_exposure, 2),
                "tax_model": "estimated_fifo_terminal_liquidation",
            },
            {
                **common, "account_profile": "ROTH_IRA",
                "estimated_tax_liability": 0.0,
                "estimated_after_tax_equity": round(final_equity, 2),
                "estimated_after_tax_return": common["pretax_return"],
                "tax_advantaged_capacity_lost_at_end": round(
                    max(0.0, config.initial_cash - final_equity), 2
                ),
                "tax_model": "qualified_distribution_assumption",
            },
        ))
    return output


def _cross_account_wash_sale_matrix(
    portfolios: dict[str, ReplayPortfolio],
) -> list[dict]:
    """Compare every taxable strategy's losses with every Roth strategy's buys."""
    output = []
    for taxable_name, taxable in sorted(portfolios.items()):
        for roth_name, roth in sorted(portfolios.items()):
            matched_loss = 0.0
            matched_shares = 0.0
            matched_events = 0
            daily_buys = defaultdict(float)
            for buy in roth.buy_events:
                day = _timestamp(buy["bought_at"]).date().toordinal()
                daily_buys[(buy["symbol"], day)] += float(buy["qty"])
            buys_by_symbol = defaultdict(list)
            for (symbol, day), quantity in daily_buys.items():
                buys_by_symbol[symbol].append([day, quantity])
            for values in buys_by_symbol.values():
                values.sort(key=lambda item: item[0])
            buy_days_by_symbol = {
                symbol: [item[0] for item in values]
                for symbol, values in buys_by_symbol.items()
            }
            for loss in taxable.loss_sale_events:
                sold_day = _timestamp(loss["sold_at"]).date().toordinal()
                remaining = float(loss["qty"])
                event_matched = False
                symbol_buys = buys_by_symbol.get(loss["symbol"], [])
                days = buy_days_by_symbol.get(loss["symbol"], [])
                start = bisect.bisect_left(days, sold_day - 30)
                end = bisect.bisect_right(days, sold_day + 30)
                for buy in symbol_buys[start:end]:
                    quantity = min(remaining, float(buy[1]))
                    if quantity <= 0:
                        continue
                    matched_shares += quantity
                    matched_loss += quantity * float(loss["loss_per_share"])
                    remaining -= quantity
                    buy[1] -= quantity
                    event_matched = True
                    if remaining <= 0:
                        break
                matched_events += int(event_matched)
            output.append({
                "taxable_strategy": taxable_name,
                "roth_strategy": roth_name,
                "matched_loss_sale_events": matched_events,
                "matched_shares": round(matched_shares, 6),
                "potential_permanently_disallowed_loss": round(matched_loss, 2),
                "window_days_before_and_after": 30,
            })
    return output


def _universe_selection_diagnostics(decisions) -> list[dict]:
    grouped = defaultdict(lambda: {
        "evaluations": 0, "selected_evaluations": 0, "raw_target_weight_sum": 0.0,
    })
    for row in decisions:
        symbol = str(row.get("symbol") or "").upper()
        key = (
            row.get("strategy_name"), symbol,
            SECTORS.get(symbol, "unclassified"),
        )
        aggregate = grouped[key]
        aggregate["evaluations"] += 1
        aggregate["selected_evaluations"] += int(bool(row.get("selected")))
        aggregate["raw_target_weight_sum"] += float(row.get("raw_target_weight") or 0.0)
    return [{
        "strategy_name": strategy, "symbol": symbol, "sector": sector,
        **aggregate,
        "selection_rate": round(
            aggregate["selected_evaluations"] / aggregate["evaluations"], 10
        ) if aggregate["evaluations"] else 0.0,
        "average_raw_target_weight": round(
            aggregate["raw_target_weight_sum"] / aggregate["evaluations"], 10
        ) if aggregate["evaluations"] else 0.0,
    } for (strategy, symbol, sector), aggregate in sorted(grouped.items())]


def _dataset_quality(rows: list[dict], symbols: list[str], benchmark_symbol: str) -> dict:
    """Measure whether each requested security has a comparable regular-session sample."""
    regular = [row for row in rows if _is_regular_session(row["timestamp"])]
    timestamps_by_session = defaultdict(set)
    # Input uniqueness is validated by load_bar_csv, so retaining a set of
    # every timestamp for every (session, symbol) duplicates hundreds of
    # thousands of references just to derive a count.
    observed = defaultdict(int)
    for row in regular:
        session_date = row["timestamp"].astimezone(MARKET_TZ).date().isoformat()
        timestamps_by_session[session_date].add(row["timestamp"])
        observed[(session_date, row["symbol"])] += 1
    requested = list(symbols)
    if benchmark_symbol and benchmark_symbol not in requested:
        requested.append(benchmark_symbol)
    details = []
    for session_date, expected in sorted(timestamps_by_session.items()):
        for symbol in requested:
            count = observed.get((session_date, symbol), 0)
            details.append({
                "session_date": session_date,
                "symbol": symbol,
                "observed_bars": count,
                "expected_bars": len(expected),
                "coverage_pct": round(count / len(expected) * 100.0, 4) if expected else 0.0,
            })
    return {
        "regular_session_rows": len(regular),
        "session_count": len(timestamps_by_session),
        "requested_symbols": requested,
        "average_coverage_pct": round(
            sum(row["coverage_pct"] for row in details) / len(details), 4
        ) if details else 0.0,
        "minimum_coverage_pct": min((row["coverage_pct"] for row in details), default=0.0),
        "coverage": details,
    }


def _portfolio_checkpoint(portfolio: ReplayPortfolio) -> dict:
    # Avoid dataclasses.asdict here: it recursively duplicates every lot and
    # tax event immediately before JSON serialization, doubling peak memory at
    # the most checkpoint-heavy part of a replay.
    payload = {
        item.name: getattr(portfolio, item.name)
        for item in fields(ReplayPortfolio)
    }
    payload["pending_at"] = portfolio.pending_at.isoformat() if portfolio.pending_at else None
    return payload


def _portfolio_from_checkpoint(payload: dict) -> ReplayPortfolio:
    values = dict(payload)
    values["pending_at"] = _timestamp(values["pending_at"]) if values.get("pending_at") else None
    values["tax_lots"] = {
        symbol: _compact_tax_lots(lots)
        for symbol, lots in dict(values.get("tax_lots") or {}).items()
    }
    values["buy_events"] = _compact_daily_tax_events(
        values.get("buy_events") or [], timestamp_key="bought_at",
    )
    values["loss_sale_events"] = _compact_daily_tax_events(
        values.get("loss_sale_events") or [], timestamp_key="sold_at",
        weighted_key="loss_per_share",
    )
    return ReplayPortfolio(**values)


def _compact_tax_lots(lots: list[dict]) -> list[dict]:
    """Merge same-day FIFO lots while retaining weighted cost and lot order."""
    compacted = []
    for source in lots:
        lot = dict(source)
        if not compacted or (
            _timestamp(compacted[-1]["acquired_at"]).date()
            != _timestamp(lot["acquired_at"]).date()
        ):
            compacted.append(lot)
            continue
        existing = compacted[-1]
        old_qty = float(existing["qty"])
        added_qty = float(lot["qty"])
        total_qty = old_qty + added_qty
        existing["cost_per_share"] = (
            old_qty * float(existing["cost_per_share"])
            + added_qty * float(lot["cost_per_share"])
        ) / total_qty
        existing["qty"] = total_qty
    return compacted


def _compact_daily_tax_events(
    events: list[dict], *, timestamp_key: str,
    weighted_key: str | None = None,
) -> list[dict]:
    """Migrate legacy per-fill checkpoint events into day-level aggregates."""
    compacted = []
    indexes = {}
    for source in events:
        event = dict(source)
        key = (
            str(event.get("symbol") or ""),
            _timestamp(event[timestamp_key]).date(),
        )
        existing_index = indexes.get(key)
        if existing_index is None:
            indexes[key] = len(compacted)
            compacted.append(event)
            continue
        existing = compacted[existing_index]
        old_qty = float(existing["qty"])
        added_qty = float(event["qty"])
        total_qty = old_qty + added_qty
        if weighted_key and total_qty:
            existing[weighted_key] = (
                old_qty * float(existing[weighted_key])
                + added_qty * float(event[weighted_key])
            ) / total_qty
        existing["qty"] = total_qty
    return compacted


def _benchmark_daily(
    rows: list[dict], benchmark_symbol: str, previous_close: float | None = None,
) -> list[dict]:
    grouped = defaultdict(list)
    for row in rows:
        if row["symbol"] == benchmark_symbol and _is_regular_session(row["timestamp"]):
            grouped[row["timestamp"].astimezone(MARKET_TZ).date().isoformat()].append(row)
    output = []
    for session_date, values in sorted(grouped.items()):
        values.sort(key=lambda row: row["timestamp"])
        first_open, last_close = float(values[0]["open"]), float(values[-1]["close"])
        start = previous_close if previous_close is not None else first_open
        output.append({
            "session_date": session_date, "symbol": benchmark_symbol,
            "start_price": start, "end_price": last_close,
            "session_return": last_close / start - 1.0 if start else 0.0,
        })
        previous_close = last_close
    return output


def _benchmark_summary(
    rows: list[dict], benchmark_symbol: str, start_price: float | None = None,
) -> dict:
    values = [
        row for row in rows
        if row["symbol"] == benchmark_symbol and _is_regular_session(row["timestamp"])
    ]
    values.sort(key=lambda row: row["timestamp"])
    if not values:
        return {"symbol": benchmark_symbol, "return": None, "start_price": None, "end_price": None}
    start, end = start_price or float(values[0]["open"]), float(values[-1]["close"])
    return {
        "symbol": benchmark_symbol, "start_price": start, "end_price": end,
        "return": round(end / start - 1.0, 10) if start else None,
    }


def _compound(returns: list[float]) -> float:
    value = 1.0
    for item in returns:
        value *= 1.0 + float(item)
    return value - 1.0


def _walk_forward_results(daily: list[dict], benchmark_daily: list[dict], folds) -> list[dict]:
    """Select on training data and report strictly held-out performance.

    SPY is an explicit investable default. A tactical strategy must beat its
    risk-adjusted score by a small margin before replacing it; this prevents a
    nearly-flat strategy from winning merely because it has negligible risk.
    """
    by_strategy = defaultdict(dict)
    daily_rows = defaultdict(dict)
    previous_equity = {}
    for row in sorted(daily, key=lambda item: (item["strategy_name"], item["session_date"])):
        strategy = row["strategy_name"]
        prior = previous_equity.get(strategy, row["first_equity"])
        by_strategy[strategy][row["session_date"]] = row["last_equity"] / prior - 1.0 if prior else 0.0
        daily_rows[strategy][row["session_date"]] = row
        previous_equity[strategy] = row["last_equity"]
    benchmark = {row["session_date"]: row["session_return"] for row in benchmark_daily}
    all_dates = sorted(benchmark)
    by_strategy["SPY_BUY_HOLD"] = dict(benchmark)
    daily_rows["SPY_BUY_HOLD"] = {
        day: {
            "session_date": day, "first_equity": 1.0,
            "last_equity": 1.0 + benchmark[day], "gross_turnover": 0.0,
        }
        for day in all_dates
    }
    by_strategy["CASH"] = {day: 0.0 for day in all_dates}
    daily_rows["CASH"] = {
        day: {
            "session_date": day, "first_equity": 1.0,
            "last_equity": 1.0, "gross_turnover": 0.0,
        }
        for day in all_dates
    }
    output = []
    for fold in folds:
        candidates = []
        for strategy, values in sorted(by_strategy.items()):
            train_returns = [values[day] for day in fold.train_dates if day in values]
            if len(train_returns) != len(fold.train_dates):
                continue
            equity = 1.0
            peak = 1.0
            max_drawdown = 0.0
            for value in train_returns:
                equity *= 1.0 + value
                peak = max(peak, equity)
                max_drawdown = min(max_drawdown, equity / peak - 1.0)
            train_rows = [daily_rows[strategy][day] for day in fold.train_dates]
            start_equity = float(train_rows[0]["first_equity"] or 0.0)
            turnover_ratio = (
                sum(float(row.get("gross_turnover") or 0.0) for row in train_rows) / start_equity
                if start_equity else 0.0
            )
            train_return = _compound(train_returns)
            chunk_size = max(1, len(train_returns) // 3)
            subwindow_returns = [
                _compound(train_returns[index:index + chunk_size])
                for index in range(0, len(train_returns), chunk_size)
            ]
            worst_subwindow_return = min(subwindow_returns, default=0.0)
            absolute_score = (
                train_return - 0.50 * abs(max_drawdown)
                - 0.0001 * turnover_ratio
                + 0.50 * min(0.0, worst_subwindow_return)
            )
            candidates.append({
                "strategy_name": strategy,
                "train_return": train_return,
                "train_daily_volatility": statistics.pstdev(train_returns) if len(train_returns) > 1 else 0.0,
                "train_max_drawdown": max_drawdown,
                "train_turnover_ratio": turnover_ratio,
                "worst_train_subwindow_return": worst_subwindow_return,
                "selection_score": absolute_score,
            })
        if not candidates:
            continue
        defensive = [
            row for row in candidates if row["strategy_name"] in {"SPY_BUY_HOLD", "CASH"}
        ]
        tactical = [
            row for row in candidates if row["strategy_name"] not in {"SPY_BUY_HOLD", "CASH"}
        ]
        selected = max(defensive, key=lambda row: (row["selection_score"], row["train_return"]))
        tactical_best = max(
            tactical, key=lambda row: (row["selection_score"], row["train_return"]),
            default=None,
        )
        # The better of SPY and CASH is the defensive baseline. A tactical
        # candidate replaces it only after clearing both risk-adjusted and
        # absolute-return hurdles on the training window.
        if tactical_best:
            score_margin = tactical_best["selection_score"] - selected["selection_score"]
            return_margin = tactical_best["train_return"] - selected["train_return"]
            if score_margin >= 0.005 and return_margin >= 0.01:
                selected = tactical_best
        test_returns = [by_strategy[selected["strategy_name"]][day] for day in fold.test_dates]
        benchmark_returns = [benchmark[day] for day in fold.test_dates if day in benchmark]
        test_return = _compound(test_returns)
        benchmark_return = _compound(benchmark_returns) if len(benchmark_returns) == len(fold.test_dates) else None
        output.append({
            **fold.as_dict(), "selected_strategy": selected["strategy_name"],
            "selection_metric": "spy_default_with_tactical_excess_hurdle_and_cash_fallback",
            "selected_train_score": round(selected["selection_score"], 10),
            "selected_train_return": round(selected["train_return"], 10),
            "selected_test_return": round(test_return, 10),
            "benchmark_test_return": round(benchmark_return, 10) if benchmark_return is not None else None,
            "test_excess_return": round(test_return - benchmark_return, 10) if benchmark_return is not None else None,
            "candidate_train_metrics": candidates,
        })
    return output


def run_replay(
    rows: list[dict], config: ReplayConfig | None = None, progress_callback=None,
    spill_directory: str | Path | None = None, initial_checkpoint: dict | None = None,
    initial_spills: dict | None = None, checkpoint_callback=None,
    checkpoint_every_sessions: int = 10, release_source_rows: bool = False,
) -> dict:
    config = config or ReplayConfig()
    if any(
        (rows[index]["timestamp"], rows[index]["symbol"])
        > (rows[index + 1]["timestamp"], rows[index + 1]["symbol"])
        for index in range(len(rows) - 1)
    ):
        rows = sorted(rows, key=lambda row: (row["timestamp"], row["symbol"]))
    all_symbols = sorted({row["symbol"] for row in rows})
    configured_universe = resolve_universe(config.universe_name)
    configured = {
        str(symbol).upper()
        for symbol in (configured_universe or config.candidate_symbols)
    }
    symbols = sorted(configured) if configured else [
        symbol for symbol in all_symbols if not (
            symbol == config.benchmark_symbol.upper() and len(all_symbols) > 1
        )
    ]
    missing_candidates = set(symbols) - set(all_symbols)
    if missing_candidates:
        raise ValueError(f"candidate symbols missing from bar data: {sorted(missing_candidates)}")
    benchmark_symbol = config.benchmark_symbol.upper()
    if config.require_benchmark and benchmark_symbol not in all_symbols:
        raise ValueError(f"required benchmark symbol missing from bar data: {benchmark_symbol}")
    quality = _dataset_quality(
        rows, symbols, benchmark_symbol if benchmark_symbol in all_symbols else ""
    )
    universe = universe_metadata(config.universe_name, symbols)
    if quality["average_coverage_pct"] < config.minimum_average_coverage_pct:
        raise ValueError(
            "average historical bar coverage is below the configured minimum: "
            f'{quality["average_coverage_pct"]:.4f}% < {config.minimum_average_coverage_pct:.4f}%'
        )
    quality["candidate_bar_age_limit_minutes"] = config.maximum_candidate_bar_age_minutes
    required_eligible_symbols = min(
        len(symbols), max(
            config.minimum_eligible_symbols,
            math.ceil(len(symbols) * config.minimum_eligible_coverage_pct / 100.0),
        ),
    )
    quality["required_eligible_symbols"] = required_eligible_symbols
    quality["minimum_eligible_coverage_pct"] = config.minimum_eligible_coverage_pct
    checkpoint = dict(initial_checkpoint or {})
    history = {
        symbol: list(dict(checkpoint.get("history") or {}).get(symbol, []))
        for symbol in all_symbols
    }
    last_observed_at = {
        symbol: _timestamp(value)
        for symbol, value in dict(checkpoint.get("last_observed_at") or {}).items()
        if symbol in all_symbols
    }
    strategy_configs = dict(STRATEGIES)
    checkpoint_strategies = set(checkpoint.get("strategy_names") or [])
    if checkpoint and checkpoint_strategies and checkpoint_strategies != set(strategy_configs):
        raise ValueError("checkpoint strategy registry does not match this replay")
    saved_portfolios = dict(checkpoint.get("portfolios") or {})
    portfolios = {
        name: _portfolio_from_checkpoint(saved_portfolios[name])
        if name in saved_portfolios else
        ReplayPortfolio(cash=config.initial_cash, peak_equity=config.initial_cash)
        for name in strategy_configs
    }
    production_builder = Layer2PortfolioBuilder()
    ranker = Layer1StockRanker(None)
    initial_spills = dict(initial_spills or {})

    def collection(name: str):
        if not spill_directory:
            return []
        restored = dict(initial_spills.get(name) or {})
        return SpilledRows(
            spill_directory, name,
            existing_path=restored.get("path"),
            existing_count=int(restored.get("count") or 0),
        )

    cycle_rows = collection("cycles")
    decision_rows = collection("decisions")
    order_rows = collection("orders")
    feature_rows = collection("features")
    eligibility_rows = collection("eligibility")
    last_decision = _timestamp(checkpoint["last_decision"]) if checkpoint.get("last_decision") else None
    cycle_id = int(checkpoint.get("cycle_id") or 0)
    prior_close_equity = dict(checkpoint.get("prior_close_equity") or {})

    regular_timestamp_count = len({
        row["timestamp"] for row in rows if _is_regular_session(row["timestamp"])
    })
    started = time.monotonic()
    completed_sessions: set[str] = set(checkpoint.get("completed_session_dates") or [])
    timestamp_index = int(checkpoint.get("completed_timestamps") or 0)
    last_processed_timestamp = (
        _timestamp(checkpoint["last_processed_timestamp"])
        if checkpoint.get("last_processed_timestamp") else None
    )
    current_session_date = None

    def replay_checkpoint(processed_at: datetime | None) -> dict:
        return {
            "version": 2,
            "strategy_names": sorted(strategy_configs),
            "portfolios": {name: _portfolio_checkpoint(value) for name, value in portfolios.items()},
            "history": history,
            "last_observed_at": {
                symbol: value.isoformat() for symbol, value in last_observed_at.items()
            },
            "last_decision": last_decision.isoformat() if last_decision else None,
            "last_processed_timestamp": processed_at.isoformat() if processed_at else None,
            "completed_timestamps": timestamp_index,
            "completed_session_dates": sorted(completed_sessions),
            "cycle_id": cycle_id,
            "prior_close_equity": prior_close_equity,
            "walk_forward_daily_history": list(checkpoint.get("walk_forward_daily_history") or []),
            "benchmark_daily_history": list(checkpoint.get("benchmark_daily_history") or []),
            "evaluation_session_dates": list(checkpoint.get("evaluation_session_dates") or []),
        }

    def persist_checkpoint(processed_at: datetime | None, *, force: bool = False) -> None:
        if not checkpoint_callback or not spill_directory:
            return
        completion_ratio = (
            timestamp_index / regular_timestamp_count if regular_timestamp_count else 1.0
        )
        adaptive_interval = _checkpoint_interval(
            completion_ratio, checkpoint_every_sessions
        )
        if not force and len(completed_sessions) % adaptive_interval:
            return
        collections = {
            "cycles": cycle_rows, "decisions": decision_rows, "orders": order_rows,
            "features": feature_rows, "eligibility": eligibility_rows,
        }
        spill_state = {name: values.checkpoint() for name, values in collections.items()}
        try:
            checkpoint_callback(replay_checkpoint(processed_at), spill_state)
        finally:
            for values in collections.values():
                values.reopen()
    grouped_rows = itertools.groupby(rows, key=lambda row: row["timestamp"])
    for ts, timestamp_rows in grouped_rows:
        if not _is_regular_session(ts):
            continue
        if last_processed_timestamp and ts <= last_processed_timestamp:
            continue
        session_date = ts.astimezone(MARKET_TZ).date().isoformat()
        if current_session_date and session_date != current_session_date:
            completed_sessions.add(current_session_date)
            persist_checkpoint(last_processed_timestamp)
        current_session_date = session_date
        timestamp_index += 1
        timestamp_rows = list(timestamp_rows)
        raw_bars = {row["symbol"]: row for row in timestamp_rows}
        bars = dict(raw_bars)
        order_rows.extend(sum((
            _execute_pending(name, portfolio, ts, raw_bars, config)
        for name, portfolio in portfolios.items()
        ), []))
        for symbol, bar in bars.items():
            last_observed_at[symbol] = ts
            history[symbol].append({key: value for key, value in bar.items() if key not in {"timestamp", "symbol"}})
            if len(history[symbol]) > max(61, config.warmup_bars):
                del history[symbol][:-max(61, config.warmup_bars)]
        last_processed_timestamp = ts
        if last_decision and (ts - last_decision) < timedelta(minutes=config.decision_interval_minutes):
            continue
        ready = {symbol: values for symbol, values in history.items() if len(values) >= config.warmup_bars}
        eligible_symbols = []
        for symbol in symbols:
            observed = last_observed_at.get(symbol)
            age_minutes = (ts - observed).total_seconds() / 60.0 if observed else None
            eligible = (
                observed is not None
                and (
                    config.maximum_candidate_bar_age_minutes is None
                    or age_minutes <= config.maximum_candidate_bar_age_minutes
                )
            )
            eligibility_rows.append({
                "timestamp": ts.isoformat(), "session_date": session_date,
                "symbol": symbol,
                "last_observed_at": observed.isoformat() if observed else None,
                "bar_age_minutes": round(age_minutes, 4) if age_minutes is not None else None,
                "eligible": eligible,
                "reason": "fresh" if eligible else "stale_or_missing",
            })
            if eligible:
                eligible_symbols.append(symbol)
        candidate_bars = {symbol: ready[symbol] for symbol in eligible_symbols if symbol in ready}
        if len(candidate_bars) < required_eligible_symbols:
            continue
        last_decision, cycle_id = ts, cycle_id + 1
        ranked = ranker.rank_from_bars(candidate_bars)
        prices = {
            symbol: float(raw_bars.get(symbol, {"close": history[symbol][-1]["close"]})["close"])
            for symbol in symbols if history.get(symbol)
        }
        if benchmark_symbol in history:
            prices[benchmark_symbol] = float(
                raw_bars.get(
                    benchmark_symbol, {"close": history[benchmark_symbol][-1]["close"]}
                )["close"]
            )
        session = source_bar_market_session_info(ts)
        production_target = production_builder.build_target_portfolio(ranked, context=session)
        rank_map = {item.symbol: (index, item.score) for index, item in enumerate(ranked, 1)}
        market_features = _market_features(candidate_bars)
        benchmark_bars = ready.get(config.benchmark_symbol.upper())
        benchmark_returns = _returns([float(item["close"]) for item in benchmark_bars]) if benchmark_bars else {}

        for symbol in candidate_bars:
            symbol_bars = candidate_bars[symbol]
            closes = [float(item["close"]) for item in symbol_bars]
            values = _returns(closes)
            one_bar_returns = [closes[index] / closes[index - 1] - 1.0 for index in range(max(1, len(closes) - 60), len(closes))]
            volumes = [float(item.get("volume", 0.0)) for item in symbol_bars]
            trades = [float(item.get("trade_count", 0.0)) for item in symbol_bars]
            rank, score = rank_map.get(symbol, (None, None))
            feature_rows.append({
                "timestamp": ts.isoformat(),
                "feature_available_at": (ts + timedelta(minutes=5)).isoformat(),
                "session_date": ts.astimezone(MARKET_TZ).date().isoformat(),
                "symbol": symbol, "rank": rank, "base_score": score,
                "source_bar_age_minutes": round(
                    (ts - last_observed_at[symbol]).total_seconds() / 60.0, 4
                ),
                "close": closes[-1], **values,
                "momentum_acceleration": values["ret_30m"] - (values["ret_60m"] - values["ret_30m"]),
                "realized_volatility_300m": statistics.stdev(one_bar_returns) if len(one_bar_returns) > 1 else 0.0,
                "volume_ratio_60m_to_300m": (sum(volumes[-12:]) / 12) / (sum(volumes[-60:]) / 60) if sum(volumes[-60:]) > 0 else 0.0,
                "trade_count_ratio_60m_to_300m": (sum(trades[-12:]) / 12) / (sum(trades[-60:]) / 60) if sum(trades[-60:]) > 0 else 0.0,
                "time_minutes_from_open": int(session.get("seconds_since_open", 0) // 60),
                **market_features,
                **{f"benchmark_{key}": value for key, value in benchmark_returns.items()},
            })

        control_equity = None
        for name, strategy_config in strategy_configs.items():
            portfolio = portfolios[name]
            if strategy_config.get("mode") == "control":
                target, decisions = production_target, []
            else:
                evaluation_config = {
                    **strategy_config,
                    "_benchmark_ret_300m": benchmark_returns.get("ret_300m", 0.0),
                }
                raw_target, decisions = _raw_research_target(
                    ranked, candidate_bars, evaluation_config
                )
                benchmark_core = safe_float(
                    strategy_config.get("benchmark_core_weight"), 0.0
                )
                if benchmark_core > 0 and benchmark_symbol in prices:
                    tactical_scale = max(0.0, 1.0 - benchmark_core)
                    raw_target = {
                        symbol: round(weight * tactical_scale, 6)
                        for symbol, weight in raw_target.items()
                        if symbol not in {"CASH", "_meta"}
                    }
                    raw_target[benchmark_symbol] = round(benchmark_core, 6)
                    raw_target["CASH"] = round(
                        max(0.0, 1.0 - sum(raw_target.values())), 6
                    )
                    raw_target["_meta"] = {
                        "strategy": "spy_core_tactical_stock_overlay",
                        "benchmark_core_weight": benchmark_core,
                    }
                volatility_target = safe_float(
                    strategy_config.get("volatility_target_annualized"), 0.0
                )
                if volatility_target > 0:
                    benchmark_closes = [
                        float(item["close"]) for item in (benchmark_bars or [])[-61:]
                    ]
                    one_bar = [
                        benchmark_closes[index] / benchmark_closes[index - 1] - 1.0
                        for index in range(1, len(benchmark_closes))
                        if benchmark_closes[index - 1]
                    ]
                    realized = (
                        statistics.pstdev(one_bar) * math.sqrt(78 * 252)
                        if len(one_bar) > 1 else 0.0
                    )
                    exposure_scale = min(
                        1.0, volatility_target / realized
                    ) if realized > 0 else 0.0
                    raw_target = {
                        symbol: round(weight * exposure_scale, 6)
                        for symbol, weight in raw_target.items()
                        if symbol not in {"CASH", "_meta"}
                    }
                    raw_target["CASH"] = round(
                        max(0.0, 1.0 - sum(raw_target.values())), 6
                    )
                    raw_target["_meta"] = {
                        "strategy": "annualized_volatility_target",
                        "volatility_target_annualized": volatility_target,
                        "realized_volatility_annualized": round(realized, 8),
                        "exposure_scale": round(exposure_scale, 8),
                    }
                minute_from_open = int(session.get("seconds_since_open", 0) // 60)
                overnight = strategy_config.get("holding_window") == "overnight"
                exit_minute = int(strategy_config.get("exit_minutes_from_open", 0))
                entry_minute = int(strategy_config.get("entry_minutes_from_open", 360))
                hold_morning = overnight and minute_from_open < exit_minute
                flat_intraday = overnight and exit_minute <= minute_from_open < entry_minute
                if hold_morning:
                    hold_equity = _equity(portfolio, prices)
                    raw_target = {
                        symbol: round(qty * prices.get(symbol, 0.0) / hold_equity, 6)
                        for symbol, qty in portfolio.positions.items()
                        if hold_equity and qty * prices.get(symbol, 0.0) / hold_equity >= 0.001
                    }
                    raw_target["CASH"] = round(
                        max(0.0, 1.0 - sum(raw_target.values())), 6
                    )
                    raw_target["_meta"] = {
                        **dict(raw_target.get("_meta") or {}),
                        "holding_window": "overnight", "schedule_state": "hold_morning",
                    }
                elif flat_intraday:
                    raw_target = {
                        "CASH": 1.0,
                        "_meta": {
                            **dict(raw_target.get("_meta") or {}),
                            "holding_window": "overnight",
                            "schedule_state": "flat_intraday",
                        },
                    }
                target = (
                    raw_target if (hold_morning or flat_intraday) else
                    _smooth_target(raw_target, portfolio.previous_target, strategy_config)
                )
            portfolio.previous_target = dict(target)
            equity = _equity(portfolio, prices)
            account = {"equity": equity, "cash": portfolio.cash, "buying_power": portfolio.cash}
            plan_result = build_layer3_shadow_plan(
                planner_source=f"REPLAY_{name}", target=target, account=account,
                positions=_position_snapshot(portfolio, prices), ranked_prices=prices,
                planner_state=portfolio.planner_state, market_is_open=True,
                cycle_id=cycle_id, bar_counts={symbol: len(candidate_bars[symbol]) for symbol in candidate_bars},
                bootstrap_eligible_symbols=set(target) - {"CASH", "_meta"},
                open_order_symbols=set(), open_order_details={}, fail_safe_active=False,
                last_trade_prices=prices, source_bar_timestamp=ts.isoformat(),
            )
            portfolio.pending_plan = list(plan_result.get("plan") or [])
            portfolio.pending_at = ts
            equity = _equity(portfolio, prices)
            portfolio.peak_equity = max(portfolio.peak_equity, equity)
            if name == "CURRENT_CONTROL":
                control_equity = equity
            reversal_symbols = {item["symbol"] for item in decisions if item.get("reversal_detected")}
            reversal_exposure = sum(portfolio.positions.get(symbol, 0.0) * prices[symbol] for symbol in reversal_symbols)
            cycle_rows.append({
                "timestamp": ts.isoformat(), "session_date": ts.astimezone(MARKET_TZ).date().isoformat(),
                "cycle_id": cycle_id, "strategy_name": name, "equity": round(equity, 2),
                "pnl": round(equity - config.initial_cash, 2),
                "vs_control": round(equity - control_equity, 2) if control_equity is not None else 0.0,
                "cash": round(portfolio.cash, 2), "cash_pct": round(portfolio.cash / equity, 8),
                "drawdown_pct": round(equity / portfolio.peak_equity - 1.0, 8),
                "peak_giveback": round(equity - portfolio.peak_equity, 2),
                "turnover": round(portfolio.turnover, 2), "trade_count": portfolio.trade_count,
                "direction_reversal_count": portfolio.reversals,
                "reversal_detected_count": len(reversal_symbols),
                "reversal_exposure_pct": round(reversal_exposure / equity, 8) if equity else None,
                "target_cash_pct": target.get("CASH"),
            })
            for item in decisions:
                decision_rows.append({"timestamp": ts.isoformat(), "cycle_id": cycle_id, "strategy_name": name, **item})

        if progress_callback and (
            timestamp_index % 25 == 0 or timestamp_index == regular_timestamp_count
        ):
            progress_callback({
                "completed_timestamps": timestamp_index,
                "total_timestamps": regular_timestamp_count,
                "completed_cycles": cycle_id,
                "completed_sessions": len(completed_sessions),
                "completed_session": session_date,
                "percent_complete": round(
                    timestamp_index / regular_timestamp_count * 100.0, 2
                ) if regular_timestamp_count else 100.0,
                "elapsed_seconds": round(time.monotonic() - started, 1),
            })

    if current_session_date:
        completed_sessions.add(current_session_date)
    persist_checkpoint(last_processed_timestamp, force=True)

    # Benchmark calculations still need the full bars. Finish them before
    # releasing fields that future labels do not use.
    prior_benchmark_history = list(checkpoint.get("benchmark_daily_history") or [])
    previous_benchmark_close = (
        float(prior_benchmark_history[-1]["end_price"])
        if prior_benchmark_history else None
    )
    benchmark_daily = _benchmark_daily(rows, benchmark_symbol, previous_benchmark_close)
    combined_benchmark_history = prior_benchmark_history + benchmark_daily
    benchmark_start_price = (
        float(combined_benchmark_history[0]["start_price"])
        if combined_benchmark_history else None
    )
    benchmark_summary = _benchmark_summary(rows, benchmark_symbol, benchmark_start_price)

    if progress_callback:
        progress_callback({"stage": "building_future_labels", "percent_complete": 100.0})
    # Replace full OHLCV rows with the four fields labels require. Keeping the
    # original rows here retained open/volume/trade/vwap values across more
    # than 500k bars and left too little memory headroom on the hosted worker.
    label_bars_by_symbol = defaultdict(list)
    for row in rows:
        label_bars_by_symbol[row["symbol"]].append(LabelBar(
            timestamp=row["timestamp"], high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
        ))
    if release_source_rows:
        rows.clear()
    dataset = _future_labels(
        feature_rows, label_bars_by_symbol, collection("dataset"),
        progress_callback=progress_callback,
    )
    label_bars_by_symbol.clear()
    if progress_callback:
        progress_callback({"stage": "building_daily_summaries"})
    daily = _daily_summaries(cycle_rows, order_rows, prior_close_equity)
    session_dates = sorted({row["session_date"] for row in feature_rows})
    if isinstance(feature_rows, SpilledRows):
        feature_rows.close()
    first_cycle_by_session = {}
    for row in cycle_rows:
        first_cycle_by_session.setdefault(row["session_date"], _timestamp(row["timestamp"]))
    evaluation_session_dates = [
        day for day in session_dates
        if first_cycle_by_session[day].astimezone(MARKET_TZ).hour == 9
        and first_cycle_by_session[day].astimezone(MARKET_TZ).minute == 30
    ]
    prior_daily_history = list(checkpoint.get("walk_forward_daily_history") or [])
    prior_evaluation_dates = list(checkpoint.get("evaluation_session_dates") or [])
    combined_daily_history = prior_daily_history + list(daily)
    combined_evaluation_dates = sorted(set(prior_evaluation_dates + evaluation_session_dates))
    folds = build_walk_forward_folds(
        combined_evaluation_dates, min_train_sessions=config.min_train_sessions,
        test_sessions=config.test_sessions, step_sessions=config.step_sessions,
    )
    all_walk_forward_results = _walk_forward_results(
        combined_daily_history, combined_benchmark_history, folds
    )
    current_evaluation_dates = set(evaluation_session_dates)
    walk_forward_results = [
        row for row in all_walk_forward_results
        if row["test_end"] in current_evaluation_dates
    ]
    current_fold_numbers = {row["fold"] for row in walk_forward_results}
    output_folds = [fold for fold in folds if fold.fold in current_fold_numbers]
    if progress_callback:
        progress_callback({"stage": "building_strategy_summaries"})
    summary_state = {
        name: {"final": None, "max_drawdown_pct": 0.0} for name in portfolios
    }
    for row in cycle_rows:
        aggregate = summary_state[row["strategy_name"]]
        aggregate["final"] = row
        aggregate["max_drawdown_pct"] = min(
            aggregate["max_drawdown_pct"], float(row["drawdown_pct"])
        )
    summaries = []
    for name, portfolio in portfolios.items():
        final = summary_state[name]["final"] or {}
        summaries.append({
            "strategy_name": name, "final_equity": final.get("equity", config.initial_cash),
            "pnl": final.get("pnl", 0.0),
            "max_drawdown_pct": summary_state[name]["max_drawdown_pct"],
            "turnover": round(portfolio.turnover, 2), "trade_count": portfolio.trade_count,
            "direction_reversal_count": portfolio.reversals,
            "pnl_with_additional_1bp_cost": round(final.get("pnl", 0.0) - portfolio.turnover * 0.0001, 2),
            "pnl_with_additional_5bp_cost": round(final.get("pnl", 0.0) - portfolio.turnover * 0.0005, 2),
            "pnl_with_additional_10bp_cost": round(final.get("pnl", 0.0) - portfolio.turnover * 0.0010, 2),
            "pnl_with_additional_20bp_cost": round(final.get("pnl", 0.0) - portfolio.turnover * 0.0020, 2),
        })
    ending_at = max(last_observed_at.values(), default=datetime.now(UTC))
    ending_prices = {
        symbol: float(values[-1]["close"])
        for symbol, values in history.items() if values
    }
    account_profiles = _account_profile_summaries(
        portfolios, summaries, ending_prices, ending_at, config
    )
    cross_account_wash_sales = _cross_account_wash_sale_matrix(portfolios)
    universe_selection = _universe_selection_diagnostics(decision_rows)
    if progress_callback:
        progress_callback({"stage": "writing_result_artifacts"})
    final_close_equity = dict(prior_close_equity)
    for row in daily:
        final_close_equity[row["strategy_name"]] = row["last_equity"]
    return {
        "config": asdict(config), "symbols": symbols, "session_dates": session_dates,
        "evaluation_session_dates": evaluation_session_dates,
        "cycles": cycle_rows, "daily": daily, "decisions": decision_rows, "orders": order_rows,
        "eligibility": eligibility_rows,
        "dataset": dataset, "walk_forward_folds": [fold.as_dict() for fold in output_folds],
        "walk_forward_results": walk_forward_results,
        "benchmark_daily": benchmark_daily, "benchmark_summary": benchmark_summary,
        "dataset_quality": quality, "universe": universe,
        "summary": summaries, "account_profiles": account_profiles,
        "cross_account_wash_sales": cross_account_wash_sales,
        "universe_selection": universe_selection,
        "checkpoint": {
            **replay_checkpoint(last_processed_timestamp),
            "prior_close_equity": final_close_equity,
            "walk_forward_daily_history": combined_daily_history,
            "benchmark_daily_history": combined_benchmark_history,
            "evaluation_session_dates": combined_evaluation_dates,
        },
    }


def _write_csv_handle(handle, rows, progress_callback=None) -> None:
    if not rows:
        return
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    total_rows = len(rows)
    for row_index, row in enumerate(rows, start=1):
        writer.writerow({key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value for key, value in row.items()})
        if progress_callback and (
            row_index == total_rows or row_index % 5000 == 0
        ):
            progress_callback(row_index, total_rows)


def _write_csv(path: Path, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        _write_csv_handle(handle, rows)


def _write_json_handle(handle, payload, heartbeat=None) -> None:
    encoder = json.JSONEncoder(indent=2, default=str)
    for chunk_index, chunk in enumerate(encoder.iterencode(payload), start=1):
        handle.write(chunk)
        if heartbeat and chunk_index % 5000 == 0:
            heartbeat()


def _account_profile_assumptions(result: dict) -> dict:
    return {
        "taxable_short_term_rate": result["config"]["taxable_short_term_rate"],
        "taxable_long_term_rate": result["config"]["taxable_long_term_rate"],
        "taxable_state_rate": result["config"]["taxable_state_rate"],
        "taxpayer_filing_status": result["config"]["taxpayer_filing_status"],
        "taxpayer_state": result["config"]["taxpayer_state"],
        "taxpayer_gross_income": result["config"]["taxpayer_gross_income"],
        "primary_taxable_profile": "CA_SINGLE_105K",
        "tax_lot_method": "FIFO",
        "taxable_terminal_value": "hypothetical liquidation at final replay price",
        "wash_sale_model": "forward 30-day same-symbol replacement estimate",
        "roth_model": "qualified-distribution tax-free assumption",
        "disclaimer": "Research estimate; not tax advice or a tax-return calculation.",
    }


def _replay_manifest(
    result: dict, *, source_path: str | Path | None,
    source_sha256: str | None, experiment: dict | None,
) -> dict:
    return {
        "created_at": datetime.now(UTC).isoformat(), "source_path": str(source_path) if source_path else None,
        "source_sha256": source_sha256 or (
            hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
            if source_path else None
        ),
        "service_mode": os.getenv("SERVICE_MODE"),
        "git_commit": os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT"),
        "git_branch": os.getenv("RENDER_GIT_BRANCH") or os.getenv("GIT_BRANCH"),
        "python_version": platform.python_version(),
        "strategy_registry_sha256": hashlib.sha256(
            json.dumps(STRATEGIES, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "experiment": experiment,
        "config": result["config"], "symbols": result["symbols"],
        "universe": result["universe"],
        "session_dates": result["session_dates"],
        "evaluation_session_dates": result["evaluation_session_dates"],
        "row_counts": {key: len(result[key]) for key in CSV_ARTIFACTS},
    }


def write_replay(
    result: dict,
    output_dir: str | Path,
    *,
    source_path: str | Path | None = None,
    source_sha256: str | None = None,
    experiment: dict | None = None,
) -> Path:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for key, filename in CSV_ARTIFACTS.items():
        _write_csv(output / filename, result[key])
    (output / "walk_forward_folds.json").write_text(json.dumps(result["walk_forward_folds"], indent=2), encoding="utf-8")
    (output / "dataset_quality.json").write_text(json.dumps(result["dataset_quality"], indent=2), encoding="utf-8")
    (output / "universe_metadata.json").write_text(json.dumps(result["universe"], indent=2), encoding="utf-8")
    (output / "replay_summary.json").write_text(json.dumps(result["summary"], indent=2), encoding="utf-8")
    (output / "benchmark_summary.json").write_text(json.dumps(result["benchmark_summary"], indent=2), encoding="utf-8")
    (output / "account_profile_assumptions.json").write_text(
        json.dumps(_account_profile_assumptions(result), indent=2), encoding="utf-8"
    )
    (output / "replay_checkpoint.json").write_text(json.dumps(result["checkpoint"], indent=2), encoding="utf-8")
    manifest = _replay_manifest(
        result, source_path=source_path, source_sha256=source_sha256,
        experiment=experiment,
    )
    (output / "replay_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return output


def write_replay_archive(
    result: dict,
    archive_path: str | Path,
    *,
    source_path: str | Path | None = None,
    source_sha256: str | None = None,
    experiment: dict | None = None,
    release_spills: bool = False,
    progress_callback=None,
) -> Path:
    """Write result artifacts directly into one ZIP with bounded local storage."""
    archive_path = Path(archive_path)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = _replay_manifest(
        result, source_path=source_path, source_sha256=source_sha256,
        experiment=experiment,
    )
    json_artifacts = {
        "walk_forward_folds.json": result["walk_forward_folds"],
        "dataset_quality.json": result["dataset_quality"],
        "universe_metadata.json": result["universe"],
        "replay_summary.json": result["summary"],
        "benchmark_summary.json": result["benchmark_summary"],
        "account_profile_assumptions.json": _account_profile_assumptions(result),
        "replay_checkpoint.json": result["checkpoint"],
        "replay_manifest.json": manifest,
    }
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1,
    ) as bundle:
        total_rows = sum(manifest["row_counts"].values())
        completed_rows = 0
        for key, filename in CSV_ARTIFACTS.items():
            rows = result[key]
            member_rows = len(rows)

            def report_member_progress(member_completed: int, _member_total: int) -> None:
                if progress_callback:
                    progress_callback({
                        "archive_member": filename,
                        "archive_completed_rows": completed_rows + member_completed,
                        "archive_total_rows": total_rows,
                        "archive_percent_complete": round(
                            (completed_rows + member_completed) / total_rows * 100.0, 2
                        ) if total_rows else 100.0,
                        "archive_bytes_written": archive_path.stat().st_size,
                    })

            with bundle.open(filename, "w") as raw_handle:
                with io.TextIOWrapper(
                    raw_handle, encoding="utf-8", newline=""
                ) as text_handle:
                    _write_csv_handle(
                        text_handle, rows,
                        progress_callback=report_member_progress,
                    )
            completed_rows += member_rows
            if release_spills and isinstance(rows, SpilledRows):
                rows.close()
                rows.path.unlink(missing_ok=True)
            if progress_callback:
                progress_callback({
                    "archive_member": filename,
                    "archive_completed_rows": completed_rows,
                    "archive_total_rows": total_rows,
                    "archive_percent_complete": round(
                        completed_rows / total_rows * 100.0, 2
                    ) if total_rows else 100.0,
                    "archive_bytes_written": archive_path.stat().st_size,
                })
        for filename, payload in json_artifacts.items():
            def report_json_progress() -> None:
                if progress_callback:
                    progress_callback({
                        "archive_member": filename,
                        "archive_completed_rows": completed_rows,
                        "archive_total_rows": total_rows,
                        "archive_percent_complete": 100.0,
                        "archive_bytes_written": archive_path.stat().st_size,
                    })

            with bundle.open(filename, "w") as raw_handle:
                with io.TextIOWrapper(raw_handle, encoding="utf-8") as text_handle:
                    _write_json_handle(
                        text_handle, payload, heartbeat=report_json_progress,
                    )
            if progress_callback:
                progress_callback({
                    "archive_member": filename,
                    "archive_completed_rows": completed_rows,
                    "archive_total_rows": total_rows,
                    "archive_percent_complete": 100.0,
                    "archive_bytes_written": archive_path.stat().st_size,
                })
    return archive_path


def main(argv=None) -> int:
    from config.service_mode import ServiceMode, validate_service_startup

    validate_service_startup(ServiceMode.HISTORICAL_RESEARCH)
    parser = argparse.ArgumentParser(description="Replay strategy variants over long-form historical five-minute OHLCV bars.")
    parser.add_argument("bars_csv")
    parser.add_argument("--output", default="replay_output")
    parser.add_argument("--initial-cash", type=float, default=100000.0)
    parser.add_argument("--spread-bps", type=float, default=1.0)
    parser.add_argument("--slippage-bps", type=float, default=1.0)
    parser.add_argument("--commission", type=float, default=0.0)
    parser.add_argument("--min-train-sessions", type=int, default=60)
    parser.add_argument("--test-sessions", type=int, default=20)
    parser.add_argument("--benchmark-symbol", default="SPY")
    parser.add_argument("--symbols", help="Comma-separated candidate symbols; defaults to every non-benchmark symbol")
    parser.add_argument("--universe", choices=("BASELINE_10", "DIVERSIFIED_20", "DIVERSIFIED_30"))
    parser.add_argument("--start-date", help="Inclusive market-session date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Inclusive market-session date (YYYY-MM-DD)")
    parser.add_argument("--short-term-tax-rate", type=float, default=0.22)
    parser.add_argument("--long-term-tax-rate", type=float, default=0.15)
    parser.add_argument("--state-tax-rate", type=float, default=0.093)
    args = parser.parse_args(argv)
    config = ReplayConfig(
        initial_cash=args.initial_cash, spread_bps=args.spread_bps,
        slippage_bps=args.slippage_bps, commission_per_order=args.commission,
        min_train_sessions=args.min_train_sessions, test_sessions=args.test_sessions,
        step_sessions=args.test_sessions,
        benchmark_symbol=args.benchmark_symbol.upper(),
        candidate_symbols=tuple(symbol.strip().upper() for symbol in (args.symbols or "").split(",") if symbol.strip()),
        universe_name=args.universe or "",
        data_start_date=args.start_date, data_end_date=args.end_date,
        taxable_short_term_rate=args.short_term_tax_rate,
        taxable_long_term_rate=args.long_term_tax_rate,
        taxable_state_rate=args.state_tax_rate,
    )
    rows = load_bar_csv(
        args.bars_csv,
        start_date=config.data_start_date,
        end_date=config.data_end_date,
        include_symbols=(
            set(resolve_universe(config.universe_name) or config.candidate_symbols)
            | {config.benchmark_symbol}
        ) if (config.universe_name or config.candidate_symbols) else None,
    )
    result = run_replay(rows, config)
    write_replay(result, args.output, source_path=args.bars_csv)
    print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
