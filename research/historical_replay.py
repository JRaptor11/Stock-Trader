from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import statistics
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from layers.layer1_ranker import Layer1StockRanker
from layers.layer2_portfolio import Layer2PortfolioBuilder
from layers.layer3_rebalancer import build_layer3_shadow_plan, source_bar_market_session_info
from layers.layer_research_strategy import STRATEGIES, _raw_research_target, _smooth_target
from research.walk_forward import build_walk_forward_folds


UTC = timezone.utc
MARKET_TZ = ZoneInfo("America/New_York")
REQUIRED_COLUMNS = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}


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
    require_benchmark: bool = True
    minimum_average_coverage_pct: float = 95.0
    bar_timestamp_semantics: str = "bar_start"

    @property
    def adverse_fill_bps(self) -> float:
        return max(0.0, self.spread_bps / 2.0 + self.slippage_bps)


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


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def load_bar_csv(path: str | Path) -> list[dict]:
    """Load long-form OHLCV bars and reject ambiguous historical input."""
    path = Path(path)
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"bar CSV missing required columns: {sorted(missing)}")
        rows, seen = [], set()
        for line, raw in enumerate(reader, start=2):
            source_timestamp = datetime.fromisoformat(
                str(raw["timestamp"]).replace("Z", "+00:00")
            )
            if source_timestamp.tzinfo is None:
                raise ValueError(f"timestamp missing UTC offset at line {line}")
            ts = _timestamp(raw["timestamp"])
            symbol = str(raw["symbol"] or "").upper().strip()
            key = (ts, symbol)
            if not symbol or key in seen:
                raise ValueError(f"invalid or duplicate bar at line {line}: {key}")
            seen.add(key)
            row = {"timestamp": ts, "symbol": symbol}
            for name in ("open", "high", "low", "close", "volume", "trade_count", "vwap"):
                value = raw.get(name)
                row[name] = float(value) if value not in (None, "") else 0.0
            if min(row["open"], row["high"], row["low"], row["close"]) <= 0:
                raise ValueError(f"non-positive OHLC value at line {line}")
            if row["high"] < max(row["open"], row["close"], row["low"]):
                raise ValueError(f"invalid high at line {line}")
            if row["low"] > min(row["open"], row["close"], row["high"]):
                raise ValueError(f"invalid low at line {line}")
            rows.append(row)
    return sorted(rows, key=lambda row: (row["timestamp"], row["symbol"]))


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


def _future_labels(features: list[dict], price_index: dict) -> list[dict]:
    output = []
    horizons = {"10m": 10, "30m": 30, "60m": 60}
    by_symbol = defaultdict(list)
    for (ts, symbol), bar in price_index.items():
        by_symbol[symbol].append((ts, bar))
    lookup = {}
    for symbol, values in by_symbol.items():
        values.sort(key=lambda item: item[0])
        value_map = {ts: bar for ts, bar in values}
        for index, (ts, _) in enumerate(values):
            lookup[(ts, symbol)] = (values, value_map, index)
    for feature in features:
        ts, symbol = _timestamp(feature["timestamp"]), feature["symbol"]
        values, value_map, index = lookup[(ts, symbol)]
        row = dict(feature)
        start = float(values[index][1]["close"])
        for label, minutes in horizons.items():
            target_ts = ts + timedelta(minutes=minutes)
            target_bar = value_map.get(target_ts)
            valid = target_bar is not None and target_ts.astimezone(MARKET_TZ).date() == ts.astimezone(MARKET_TZ).date()
            row[f"forward_return_{label}"] = (
                round(float(target_bar["close"]) / start - 1.0, 10) if valid else None
            )
        window_end = ts + timedelta(minutes=60)
        window = [
            item[1] for item in values[index + 1:]
            if item[0] <= window_end
            and item[0].astimezone(MARKET_TZ).date() == ts.astimezone(MARKET_TZ).date()
        ]
        row["max_favorable_excursion_60m"] = (
            round(max(float(item["high"]) for item in window) / start - 1.0, 10) if window else None
        )
        row["max_adverse_excursion_60m"] = (
            round(min(float(item["low"]) for item in window) / start - 1.0, 10) if window else None
        )
        row["label_available_60m"] = row["forward_return_60m"] is not None
        output.append(row)
    return output


def _daily_summaries(cycles: list[dict], orders: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    order_grouped = defaultdict(list)
    for row in cycles:
        grouped[(row["session_date"], row["strategy_name"])].append(row)
    for row in orders:
        session_date = _timestamp(row["timestamp"]).astimezone(MARKET_TZ).date().isoformat()
        order_grouped[(session_date, row["strategy_name"])].append(row)
    output = []
    for (session_date, strategy), values in sorted(grouped.items()):
        values.sort(key=lambda row: row["timestamp"])
        trades = order_grouped.get((session_date, strategy), [])
        start, end = values[0], values[-1]
        output.append({
            "session_date": session_date, "strategy_name": strategy,
            "first_equity": start["equity"], "last_equity": end["equity"],
            "intraday_pnl": round(end["equity"] - start["equity"], 2),
            "intraday_return": round(end["equity"] / start["equity"] - 1.0, 10) if start["equity"] else None,
            "maximum_drawdown_pct": min(row["drawdown_pct"] for row in values),
            "peak_giveback": min(row["peak_giveback"] for row in values),
            "trade_count": len(trades),
            "gross_turnover": round(sum(float(row["notional"]) for row in trades), 2),
            "ending_cash_pct": end["cash_pct"],
            "ending_reversal_exposure_pct": end["reversal_exposure_pct"],
        })
    return output


def _dataset_quality(rows: list[dict], symbols: list[str], benchmark_symbol: str) -> dict:
    """Measure whether each requested security has a comparable regular-session sample."""
    regular = [row for row in rows if _is_regular_session(row["timestamp"])]
    timestamps_by_session = defaultdict(set)
    observed = defaultdict(set)
    for row in regular:
        session_date = row["timestamp"].astimezone(MARKET_TZ).date().isoformat()
        timestamps_by_session[session_date].add(row["timestamp"])
        observed[(session_date, row["symbol"])].add(row["timestamp"])
    requested = list(symbols)
    if benchmark_symbol and benchmark_symbol not in requested:
        requested.append(benchmark_symbol)
    details = []
    for session_date, expected in sorted(timestamps_by_session.items()):
        for symbol in requested:
            count = len(observed.get((session_date, symbol), set()))
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


def _benchmark_daily(rows: list[dict], benchmark_symbol: str) -> list[dict]:
    grouped = defaultdict(list)
    for row in rows:
        if row["symbol"] == benchmark_symbol and _is_regular_session(row["timestamp"]):
            grouped[row["timestamp"].astimezone(MARKET_TZ).date().isoformat()].append(row)
    output = []
    previous_close = None
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


def _benchmark_summary(rows: list[dict], benchmark_symbol: str) -> dict:
    values = [
        row for row in rows
        if row["symbol"] == benchmark_symbol and _is_regular_session(row["timestamp"])
    ]
    values.sort(key=lambda row: row["timestamp"])
    if not values:
        return {"symbol": benchmark_symbol, "return": None, "start_price": None, "end_price": None}
    start, end = float(values[0]["open"]), float(values[-1]["close"])
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
    """Select on each training window, then report only held-out test performance."""
    by_strategy = defaultdict(dict)
    previous_equity = {}
    for row in sorted(daily, key=lambda item: (item["strategy_name"], item["session_date"])):
        strategy = row["strategy_name"]
        prior = previous_equity.get(strategy, row["first_equity"])
        by_strategy[strategy][row["session_date"]] = row["last_equity"] / prior - 1.0 if prior else 0.0
        previous_equity[strategy] = row["last_equity"]
    benchmark = {row["session_date"]: row["session_return"] for row in benchmark_daily}
    output = []
    for fold in folds:
        candidates = []
        for strategy, values in sorted(by_strategy.items()):
            train_returns = [values[day] for day in fold.train_dates if day in values]
            if len(train_returns) != len(fold.train_dates):
                continue
            candidates.append({
                "strategy_name": strategy,
                "train_return": _compound(train_returns),
                "train_daily_volatility": statistics.pstdev(train_returns) if len(train_returns) > 1 else 0.0,
            })
        if not candidates:
            continue
        selected = max(candidates, key=lambda row: (row["train_return"], -row["train_daily_volatility"]))
        test_returns = [by_strategy[selected["strategy_name"]][day] for day in fold.test_dates]
        benchmark_returns = [benchmark[day] for day in fold.test_dates if day in benchmark]
        test_return = _compound(test_returns)
        benchmark_return = _compound(benchmark_returns) if len(benchmark_returns) == len(fold.test_dates) else None
        output.append({
            **fold.as_dict(), "selected_strategy": selected["strategy_name"],
            "selection_metric": "highest_compounded_train_return_after_configured_costs",
            "selected_train_return": round(selected["train_return"], 10),
            "selected_test_return": round(test_return, 10),
            "benchmark_test_return": round(benchmark_return, 10) if benchmark_return is not None else None,
            "test_excess_return": round(test_return - benchmark_return, 10) if benchmark_return is not None else None,
            "candidate_train_metrics": candidates,
        })
    return output


def run_replay(rows: list[dict], config: ReplayConfig | None = None, progress_callback=None) -> dict:
    config = config or ReplayConfig()
    grouped, price_index = defaultdict(dict), {}
    for row in rows:
        grouped[row["timestamp"]][row["symbol"]] = row
        price_index[(row["timestamp"], row["symbol"])] = row
    all_symbols = sorted({row["symbol"] for row in rows})
    configured = {str(symbol).upper() for symbol in config.candidate_symbols}
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
    if quality["average_coverage_pct"] < config.minimum_average_coverage_pct:
        raise ValueError(
            "average historical bar coverage is below the configured minimum: "
            f'{quality["average_coverage_pct"]:.4f}% < {config.minimum_average_coverage_pct:.4f}%'
        )
    history = {symbol: [] for symbol in all_symbols}
    strategy_configs = dict(STRATEGIES)
    portfolios = {
        name: ReplayPortfolio(cash=config.initial_cash, peak_equity=config.initial_cash)
        for name in strategy_configs
    }
    production_builder = Layer2PortfolioBuilder()
    ranker = Layer1StockRanker(None)
    cycle_rows, decision_rows, order_rows, feature_rows = [], [], [], []
    last_decision = None
    cycle_id = 0

    timestamps = [ts for ts in sorted(grouped) if _is_regular_session(ts)]
    started = time.monotonic()
    completed_sessions: set[str] = set()
    for timestamp_index, ts in enumerate(timestamps, 1):
        bars = grouped[ts]
        if not _is_regular_session(ts):
            continue
        order_rows.extend(sum((
            _execute_pending(name, portfolio, ts, bars, config)
        for name, portfolio in portfolios.items()
        ), []))
        for symbol, bar in bars.items():
            history[symbol].append({key: value for key, value in bar.items() if key not in {"timestamp", "symbol"}})
        if last_decision and (ts - last_decision) < timedelta(minutes=config.decision_interval_minutes):
            continue
        ready = {symbol: values for symbol, values in history.items() if len(values) >= config.warmup_bars}
        candidate_bars = {symbol: ready[symbol] for symbol in symbols if symbol in ready}
        if len(candidate_bars) != len(symbols):
            continue
        last_decision, cycle_id = ts, cycle_id + 1
        ranked = ranker.rank_from_bars(candidate_bars)
        prices = {symbol: float(bars.get(symbol, {"close": candidate_bars[symbol][-1]["close"]})["close"]) for symbol in symbols}
        session = source_bar_market_session_info(ts)
        production_target = production_builder.build_target_portfolio(ranked, context=session)
        rank_map = {item.symbol: (index, item.score) for index, item in enumerate(ranked, 1)}
        market_features = _market_features(candidate_bars)
        benchmark_bars = ready.get(config.benchmark_symbol.upper())
        benchmark_returns = _returns([float(item["close"]) for item in benchmark_bars]) if benchmark_bars else {}

        for symbol in symbols:
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
                raw_target, decisions = _raw_research_target(ranked, candidate_bars, strategy_config)
                target = _smooth_target(raw_target, portfolio.previous_target, strategy_config)
            portfolio.previous_target = dict(target)
            equity = _equity(portfolio, prices)
            account = {"equity": equity, "cash": portfolio.cash, "buying_power": portfolio.cash}
            plan_result = build_layer3_shadow_plan(
                planner_source=f"REPLAY_{name}", target=target, account=account,
                positions=_position_snapshot(portfolio, prices), ranked_prices=prices,
                planner_state=portfolio.planner_state, market_is_open=True,
                cycle_id=cycle_id, bar_counts={symbol: len(candidate_bars[symbol]) for symbol in symbols},
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

        session_date = ts.astimezone(MARKET_TZ).date().isoformat()
        completed_sessions.add(session_date)
        if progress_callback and (timestamp_index % 25 == 0 or timestamp_index == len(timestamps)):
            progress_callback({
                "completed_timestamps": timestamp_index,
                "total_timestamps": len(timestamps),
                "completed_cycles": cycle_id,
                "completed_sessions": len(completed_sessions),
                "completed_session": session_date,
                "percent_complete": round(timestamp_index / len(timestamps) * 100.0, 2) if timestamps else 100.0,
                "elapsed_seconds": round(time.monotonic() - started, 1),
            })

    dataset = _future_labels(feature_rows, price_index)
    daily = _daily_summaries(cycle_rows, order_rows)
    session_dates = sorted({row["session_date"] for row in feature_rows})
    first_cycle_by_session = {}
    for row in cycle_rows:
        first_cycle_by_session.setdefault(row["session_date"], _timestamp(row["timestamp"]))
    evaluation_session_dates = [
        day for day in session_dates
        if first_cycle_by_session[day].astimezone(MARKET_TZ).hour == 9
        and first_cycle_by_session[day].astimezone(MARKET_TZ).minute == 30
    ]
    folds = build_walk_forward_folds(
        evaluation_session_dates, min_train_sessions=config.min_train_sessions,
        test_sessions=config.test_sessions, step_sessions=config.step_sessions,
    )
    benchmark_daily = _benchmark_daily(rows, benchmark_symbol)
    benchmark_summary = _benchmark_summary(rows, benchmark_symbol)
    walk_forward_results = _walk_forward_results(daily, benchmark_daily, folds)
    summaries = []
    for name, portfolio in portfolios.items():
        relevant = [row for row in cycle_rows if row["strategy_name"] == name]
        final = relevant[-1] if relevant else {}
        summaries.append({
            "strategy_name": name, "final_equity": final.get("equity", config.initial_cash),
            "pnl": final.get("pnl", 0.0), "max_drawdown_pct": min((row["drawdown_pct"] for row in relevant), default=0.0),
            "turnover": round(portfolio.turnover, 2), "trade_count": portfolio.trade_count,
            "direction_reversal_count": portfolio.reversals,
            "pnl_with_additional_1bp_cost": round(final.get("pnl", 0.0) - portfolio.turnover * 0.0001, 2),
            "pnl_with_additional_5bp_cost": round(final.get("pnl", 0.0) - portfolio.turnover * 0.0005, 2),
            "pnl_with_additional_10bp_cost": round(final.get("pnl", 0.0) - portfolio.turnover * 0.0010, 2),
            "pnl_with_additional_20bp_cost": round(final.get("pnl", 0.0) - portfolio.turnover * 0.0020, 2),
        })
    return {
        "config": asdict(config), "symbols": symbols, "session_dates": session_dates,
        "evaluation_session_dates": evaluation_session_dates,
        "cycles": cycle_rows, "daily": daily, "decisions": decision_rows, "orders": order_rows,
        "dataset": dataset, "walk_forward_folds": [fold.as_dict() for fold in folds],
        "walk_forward_results": walk_forward_results,
        "benchmark_daily": benchmark_daily, "benchmark_summary": benchmark_summary,
        "dataset_quality": quality,
        "summary": summaries,
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value for key, value in row.items()})


def write_replay(
    result: dict,
    output_dir: str | Path,
    *,
    source_path: str | Path | None = None,
    experiment: dict | None = None,
) -> Path:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for key, filename in {
        "cycles": "replay_cycles.csv", "daily": "replay_daily.csv", "decisions": "replay_decisions.csv",
        "orders": "replay_orders.csv", "dataset": "ml_dataset.csv",
        "benchmark_daily": "benchmark_daily.csv",
        "walk_forward_results": "walk_forward_results.csv",
    }.items():
        _write_csv(output / filename, result[key])
    (output / "walk_forward_folds.json").write_text(json.dumps(result["walk_forward_folds"], indent=2), encoding="utf-8")
    (output / "dataset_quality.json").write_text(json.dumps(result["dataset_quality"], indent=2), encoding="utf-8")
    (output / "replay_summary.json").write_text(json.dumps(result["summary"], indent=2), encoding="utf-8")
    (output / "benchmark_summary.json").write_text(json.dumps(result["benchmark_summary"], indent=2), encoding="utf-8")
    manifest = {
        "created_at": datetime.now(UTC).isoformat(), "source_path": str(source_path) if source_path else None,
        "source_sha256": hashlib.sha256(Path(source_path).read_bytes()).hexdigest() if source_path else None,
        "service_mode": os.getenv("SERVICE_MODE"),
        "git_commit": os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT"),
        "git_branch": os.getenv("RENDER_GIT_BRANCH") or os.getenv("GIT_BRANCH"),
        "python_version": platform.python_version(),
        "strategy_registry_sha256": hashlib.sha256(
            json.dumps(STRATEGIES, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "experiment": experiment,
        "config": result["config"], "symbols": result["symbols"],
        "session_dates": result["session_dates"],
        "evaluation_session_dates": result["evaluation_session_dates"], "row_counts": {
            key: len(result[key]) for key in (
                "cycles", "daily", "decisions", "orders", "dataset",
                "benchmark_daily", "walk_forward_results",
            )
        },
    }
    (output / "replay_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return output


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
    args = parser.parse_args(argv)
    config = ReplayConfig(
        initial_cash=args.initial_cash, spread_bps=args.spread_bps,
        slippage_bps=args.slippage_bps, commission_per_order=args.commission,
        min_train_sessions=args.min_train_sessions, test_sessions=args.test_sessions,
        step_sessions=args.test_sessions,
        benchmark_symbol=args.benchmark_symbol.upper(),
        candidate_symbols=tuple(symbol.strip().upper() for symbol in (args.symbols or "").split(",") if symbol.strip()),
    )
    rows = load_bar_csv(args.bars_csv)
    result = run_replay(rows, config)
    write_replay(result, args.output, source_path=args.bars_csv)
    print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
