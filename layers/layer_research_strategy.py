from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timezone

from core.state import app_state
from layers.layer3_rebalancer import build_layer3_shadow_plan
from layers.layer_csv import (
    append_layer_research_strategy_cycle_rows,
    append_layer_research_strategy_decision_rows,
    append_layer_research_strategy_order_rows,
    append_layer_research_strategy_portfolio_rows,
)
from utils.numeric import safe_float, safe_int


STRATEGIES = {
    "CURRENT_CONTROL": {"mode": "control"},
    "REDESIGN_CONSERVATIVE": {
        "target_mode": "legacy", "alpha": 0.20, "max_step": 0.035,
    },
    "REDESIGN_RESPONSIVE": {
        "target_mode": "legacy", "alpha": 0.40, "max_step": 0.075,
    },
    # These variants intentionally share smoothing/execution settings. Their
    # results therefore isolate target-signal timing instead of conflating a
    # shorter lookback with a faster rebalance policy.
    "LOOKBACK_60M": {
        "target_mode": "single_horizon", "horizon_minutes": 60,
        "alpha": 0.40, "max_step": 0.075,
    },
    "LOOKBACK_150M": {
        "target_mode": "single_horizon", "horizon_minutes": 150,
        "alpha": 0.40, "max_step": 0.075,
    },
    "LOOKBACK_300M": {
        "target_mode": "single_horizon", "horizon_minutes": 300,
        "alpha": 0.40, "max_step": 0.075,
    },
    "MULTI_HORIZON_BLEND": {
        "target_mode": "blend", "alpha": 0.40, "max_step": 0.075,
    },
    "ADAPTIVE_REVERSAL": {
        "target_mode": "adaptive", "alpha": 0.40, "max_step": 0.075,
    },
}

def _return(closes: list[float], bars: int) -> float:
    if len(closes) <= bars or not closes[-bars - 1]:
        return 0.0
    return (closes[-1] - closes[-bars - 1]) / closes[-bars - 1]


def _volatility(closes: list[float], bars: int = 60) -> float:
    recent = closes[-(bars + 1):]
    if len(recent) < 3:
        return 0.0
    returns = [
        (recent[i] - recent[i - 1]) / recent[i - 1]
        for i in range(1, len(recent)) if recent[i - 1]
    ]
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    return math.sqrt(sum((value - mean) ** 2 for value in returns) / (len(returns) - 1))


def _rank_map(ranked) -> dict[str, dict]:
    out = {}
    for index, item in enumerate(ranked or [], start=1):
        symbol = str(getattr(item, "symbol", "") or "").upper()
        if symbol:
            out[symbol] = {
                "rank": index,
                "score": safe_float(getattr(item, "score", 0.0), 0.0),
                "price": safe_float(getattr(item, "last_price", 0.0), 0.0),
            }
    return out


def _strategy_signal(
    *, base_score: float, ret_30m: float, ret_60m: float,
    ret_150m: float, ret_300m: float, acceleration: float,
    config: dict,
) -> tuple[float, str, int, bool]:
    """Return a comparable absolute signal and timing diagnostics."""
    mode = config.get("target_mode", "legacy")
    returns = {60: ret_60m, 150: ret_150m, 300: ret_300m}
    agreement = sum(value > 0 for value in returns.values())
    reversal = ret_30m < 0 and ret_60m < 0 and acceleration < 0
    if mode == "single_horizon":
        horizon = int(config["horizon_minutes"])
        return returns[horizon], f"{horizon}m", agreement, reversal
    if mode == "blend":
        # Recent information has the greatest influence, while the longer
        # windows prevent a single noisy 5-minute move from dominating.
        signal = 0.50 * ret_60m + 0.30 * ret_150m + 0.20 * ret_300m
        return signal, "60/150/300_blend", agreement, reversal
    if mode == "adaptive":
        if reversal or (ret_60m * ret_300m < 0):
            signal = 0.65 * ret_60m + 0.25 * ret_150m + 0.10 * ret_300m
            label = "adaptive_recent"
        else:
            signal = 0.25 * ret_60m + 0.35 * ret_150m + 0.40 * ret_300m
            label = "adaptive_stable"
        return signal, label, agreement, reversal
    return base_score, "production_composite", agreement, reversal


def _raw_research_target(
    ranked, bars_by_symbol: dict, config: dict | None = None,
) -> tuple[dict, list[dict]]:
    config = dict(config or {"target_mode": "legacy"})
    rank_map = _rank_map(ranked)
    decisions = []
    qualified = []

    for symbol, info in rank_map.items():
        bars = list(bars_by_symbol.get(symbol, []) or [])
        closes = [safe_float(bar.get("close"), 0.0) for bar in bars]
        ret_6 = _return(closes, 6)
        ret_12 = _return(closes, 12)
        ret_30 = _return(closes, 30)
        ret_60 = _return(closes, 60)
        prior_30m = ret_12 - ret_6
        acceleration = ret_6 - prior_30m
        volatility = _volatility(closes)
        base_score = info["score"]

        signal_score, signal_horizon, horizon_agreement, reversal_detected = (
            _strategy_signal(
                base_score=base_score, ret_30m=ret_6, ret_60m=ret_12,
                ret_150m=ret_30, ret_300m=ret_60,
                acceleration=acceleration, config=config,
            )
        )

        mode = config.get("target_mode", "legacy")
        if mode == "legacy":
            positive_absolute_signal = base_score >= 0.001 and ret_60 > 0.0
        elif mode in {"blend", "adaptive"}:
            positive_absolute_signal = signal_score >= 0.001 and horizon_agreement >= 2
        else:
            positive_absolute_signal = signal_score >= 0.001
        severe_deterioration = ret_6 <= -0.003 and ret_12 <= -0.005
        moderate_deterioration = ret_6 < 0 and ret_12 < 0 and acceleration < 0

        if severe_deterioration:
            multiplier = 0.0
            reason = "rejected_severe_recent_deterioration"
        elif not positive_absolute_signal:
            multiplier = 0.0
            reason = "rejected_absolute_qualification"
        elif moderate_deterioration:
            multiplier = 0.35
            reason = "qualified_reduced_recent_deterioration"
        elif ret_6 > 0 and ret_12 > 0:
            multiplier = 1.0
            reason = "qualified_recent_confirmation"
        else:
            multiplier = 0.70
            reason = "qualified_mixed_recent_confirmation"

        effective_score = max(0.0, signal_score) * multiplier
        row = {
            "symbol": symbol, "production_rank": info["rank"],
            "base_score": base_score, "ret_30m": ret_6, "ret_60m": ret_12,
            "ret_150m": ret_30, "ret_300m": ret_60,
            "signal_horizon": signal_horizon, "signal_score": signal_score,
            "horizon_agreement_count": horizon_agreement,
            "reversal_detected": reversal_detected,
            "momentum_acceleration": acceleration, "volatility_300m": volatility,
            "positive_absolute_signal": positive_absolute_signal,
            "severe_deterioration": severe_deterioration,
            "moderate_deterioration": moderate_deterioration,
            "qualification_multiplier": multiplier,
            "effective_score": effective_score, "qualification_reason": reason,
            "qualified": effective_score > 0,
        }
        decisions.append(row)
        if effective_score > 0:
            qualified.append(row)

    qualified.sort(key=lambda row: row["effective_score"], reverse=True)
    selected = qualified[:5]
    selected_symbols = {row["symbol"] for row in selected}

    if not selected:
        target = {"CASH": 1.0}
    else:
        average_score = sum(row["effective_score"] for row in selected) / len(selected)
        strength = max(0.0, min(1.0, average_score / 0.04))
        breadth = len(selected) / 5.0
        average_volatility = sum(row["volatility_300m"] for row in selected) / len(selected)
        volatility_factor = max(0.50, min(1.0, 1.0 - average_volatility * 12.0))
        investable = min(0.90, 0.85 * strength * breadth * volatility_factor)
        investable = max(0.0, investable)
        total_score = sum(row["effective_score"] for row in selected)
        raw_weights = {
            row["symbol"]: investable * row["effective_score"] / total_score
            for row in selected
        }
        capped = {symbol: min(0.30, weight) for symbol, weight in raw_weights.items()}
        unused = investable - sum(capped.values())
        uncapped = [symbol for symbol, weight in raw_weights.items() if weight < 0.30]
        while unused > 0.000001 and uncapped:
            addition = unused / len(uncapped)
            next_uncapped = []
            for symbol in uncapped:
                room = 0.30 - capped[symbol]
                applied = min(room, addition)
                capped[symbol] += applied
                unused -= applied
                if capped[symbol] < 0.30 - 0.000001:
                    next_uncapped.append(symbol)
            uncapped = next_uncapped
        target = {symbol: round(weight, 6) for symbol, weight in capped.items() if weight >= 0.01}
        target["CASH"] = round(1.0 - sum(target.values()), 6)

    for row in decisions:
        row["selected"] = row["symbol"] in selected_symbols
        row["raw_target_weight"] = safe_float(target.get(row["symbol"]), 0.0)

    target["_meta"] = {
        "strategy": "redesigned_absolute_momentum",
        "target_mode": config.get("target_mode", "legacy"),
        "horizon_minutes": config.get("horizon_minutes"),
        "qualified_count": len(qualified), "selected_count": len(selected),
        "rejected_count": len(decisions) - len(qualified),
        "deteriorating_count": sum(1 for row in decisions if row["severe_deterioration"] or row["moderate_deterioration"]),
        "weighting_mode": "absolute_confidence",
    }
    return target, decisions


def _smooth_target(raw_target: dict, previous: dict | None, config: dict) -> dict:
    if not previous:
        return dict(raw_target)
    symbols = {
        key for key in set(raw_target) | set(previous)
        if not str(key).startswith("_") and key != "CASH"
    }
    alpha = config["alpha"]
    max_step = config["max_step"]
    weights = {}
    for symbol in symbols:
        old = safe_float(previous.get(symbol), 0.0)
        raw = safe_float(raw_target.get(symbol), 0.0)
        desired = old + alpha * (raw - old)
        value = max(old - max_step, min(old + max_step, desired))
        if value >= 0.005:
            weights[symbol] = value
    total = sum(weights.values())
    if total > 0.95:
        scale = 0.95 / total
        weights = {symbol: value * scale for symbol, value in weights.items()}
    target = {symbol: round(value, 6) for symbol, value in weights.items()}
    target["CASH"] = round(1.0 - sum(target.values()), 6)
    target["_meta"] = {
        **dict(raw_target.get("_meta") or {}),
        "smoothing_alpha": alpha, "smoothing_max_step": max_step,
        "smoothing_mode": "research_path_dependent",
    }
    return target


def _prices(layer3_plan: list[dict], ranked) -> dict[str, float]:
    prices = {
        symbol: info["price"] for symbol, info in _rank_map(ranked).items()
        if info["price"] > 0
    }
    for row in layer3_plan or []:
        symbol = str(row.get("symbol") or "").upper()
        price = safe_float(row.get("live_price") or row.get("price"), 0.0)
        if symbol and price > 0:
            prices[symbol] = price
    return prices


def _equity(portfolio: dict, prices: dict) -> float:
    return safe_float(portfolio.get("cash"), 0.0) + sum(
        safe_float(qty, 0.0) * safe_float(prices.get(symbol), 0.0)
        for symbol, qty in portfolio.get("positions", {}).items()
    )


def _initialize(state: dict, layer3_plan: list[dict], summary: dict, prices: dict) -> None:
    positions = {
        str(row.get("symbol") or "").upper(): safe_float(row.get("current_qty"), 0.0)
        for row in layer3_plan or [] if safe_float(row.get("current_qty"), 0.0) > 0
    }
    cash = safe_float(summary.get("cash"), 0.0)
    if cash <= 0:
        cash = max(0.0, safe_float(summary.get("equity"), 0.0) - sum(
            qty * safe_float(prices.get(symbol), 0.0) for symbol, qty in positions.items()
        ))
    state["portfolios"] = {}
    for name in STRATEGIES:
        portfolio = {
            "cash": cash, "positions": dict(positions), "planner_state": {},
            "previous_target": None, "cumulative_turnover": 0.0,
            "cumulative_trade_count": 0, "follow_up_trade_count": 0,
            "direction_reversal_count": 0, "last_trade_side": {},
            "traded_sides": {},
        }
        portfolio["initial_equity"] = _equity(portfolio, prices)
        portfolio["peak_equity"] = portfolio["initial_equity"]
        state["portfolios"][name] = portfolio
    state["initialized"] = True


def _apply_plan(portfolio: dict, plan: list[dict], prices: dict, *, timestamp: str, cycle_id) -> list[dict]:
    rows = []
    for row in plan or []:
        decision = str(row.get("decision") or "").upper()
        if decision not in {"BUY", "SELL"}:
            continue
        symbol = str(row.get("symbol") or "").upper()
        price = safe_float(row.get("live_price") or prices.get(symbol), 0.0)
        requested = math.floor(safe_float(row.get("planned_qty"), 0.0))
        before_qty = safe_float(portfolio["positions"].get(symbol), 0.0)
        cash_before = portfolio["cash"]
        status = "executed"
        if decision == "BUY":
            qty = min(requested, math.floor(portfolio["cash"] / price)) if price > 0 else 0
            if qty > 0:
                portfolio["positions"][symbol] = before_qty + qty
                portfolio["cash"] -= qty * price
            else:
                status = "skipped_cash_or_price"
        else:
            qty = min(requested, math.floor(before_qty))
            if qty > 0:
                portfolio["positions"][symbol] = before_qty - qty
                portfolio["cash"] += qty * price
                if portfolio["positions"][symbol] <= 0:
                    portfolio["positions"].pop(symbol, None)
            else:
                status = "skipped_no_position"
        notional = qty * price
        if status == "executed":
            portfolio["cumulative_turnover"] += notional
            portfolio["cumulative_trade_count"] += 1
            prior_side = portfolio["last_trade_side"].get(symbol)
            is_follow_up = prior_side is not None
            if is_follow_up:
                portfolio["follow_up_trade_count"] += 1
            if prior_side and prior_side != decision:
                portfolio["direction_reversal_count"] += 1
            portfolio["last_trade_side"][symbol] = decision
            portfolio["traded_sides"].setdefault(symbol, set()).add(decision)
        else:
            is_follow_up = False
        rows.append({
            "timestamp": timestamp, "cycle_id": cycle_id, "symbol": symbol,
            "side": decision.lower(), "status": status, "qty": qty,
            "price": price, "notional": notional, "requested_qty": requested,
            "cash_before": cash_before, "cash_after": portfolio["cash"],
            "position_qty_before": before_qty,
            "position_qty_after": safe_float(portfolio["positions"].get(symbol), 0.0),
            "reason": row.get("reason"), "is_follow_up": is_follow_up,
        })
    return rows


def run_research_strategy_shadow(
    *, ranked, production_target: dict, bars_by_symbol: dict,
    layer3_plan: list[dict], layer3_summary: dict, source_bar_timestamp,
) -> dict:
    """Run redesigned portfolios without changing production targets/orders."""
    if app_state.get("execution", {}).get("research_strategy_shadow_enabled", True) is False:
        return {"status": "disabled"}
    if layer3_summary.get("status") != "ok":
        return {"status": "skipped", "reason": "production_layer3_not_ok"}
    timestamp = datetime.now(timezone.utc).isoformat()
    cycle_id = layer3_summary.get("cycle_id")
    state = app_state.setdefault("layers", {}).setdefault("research_strategy_shadow", {})
    session_date = str(layer3_summary.get("open_session_date") or timestamp[:10])
    if state.get("date") != session_date:
        state.clear()
        state["date"] = session_date
    prices = _prices(layer3_plan, ranked)
    if not state.get("initialized"):
        _initialize(state, layer3_plan, layer3_summary, prices)

    config_hash = hashlib.sha256(json.dumps({"strategies": STRATEGIES, "rules": "v2"}, sort_keys=True).encode()).hexdigest()[:16]
    cycle_rows, decision_rows, order_rows, portfolio_rows = [], [], [], []

    control_equity = None
    for name, config in STRATEGIES.items():
        portfolio = state["portfolios"][name]
        if config.get("mode") == "control":
            raw_target, base_decisions = {"_meta": {}}, []
            target = dict(production_target or {})
        else:
            raw_target, base_decisions = _raw_research_target(
                ranked, bars_by_symbol, config,
            )
            target = _smooth_target(
                raw_target, portfolio.get("previous_target"), config,
            )
        portfolio["previous_target"] = dict(target)
        account = {
            "source": name.lower(), "broker_snapshot_ok": True,
            "equity": _equity(portfolio, prices), "cash": portfolio["cash"],
            "buying_power": portfolio["cash"],
        }
        positions = {
            symbol: {"symbol": symbol, "qty": qty, "current_price": prices.get(symbol),
                     "market_value": qty * safe_float(prices.get(symbol), 0.0),
                     "avg_entry_price": prices.get(symbol), "unrealized_plpc": 0.0}
            for symbol, qty in portfolio["positions"].items()
        }
        planner = build_layer3_shadow_plan(
            planner_source=name, target=target, account=account, positions=positions,
            ranked_prices=prices, planner_state=portfolio["planner_state"],
            market_is_open=True, cycle_id=safe_int(cycle_id, 0),
            bar_counts={symbol: len(bars or []) for symbol, bars in bars_by_symbol.items()},
            bootstrap_eligible_symbols=set(production_target or {}) - {"CASH", "_meta"},
            open_order_symbols=set(), open_order_details={}, fail_safe_active=False,
            last_trade_prices=prices, source_bar_timestamp=source_bar_timestamp,
        )
        variant_orders = _apply_plan(
            portfolio, planner.get("plan", []), prices,
            timestamp=timestamp, cycle_id=cycle_id,
        )
        for row in variant_orders:
            row["strategy_name"] = name
        order_rows.extend(variant_orders)

        equity = _equity(portfolio, prices)
        if name == "CURRENT_CONTROL":
            control_equity = equity
        portfolio["peak_equity"] = max(portfolio["peak_equity"], equity)
        drawdown = (equity - portfolio["peak_equity"]) / portfolio["peak_equity"] if portfolio["peak_equity"] else 0.0
        pnl = equity - portfolio["initial_equity"]
        turnover = portfolio["cumulative_turnover"]
        meta = raw_target.get("_meta", {})
        reversal_symbols = {
            row["symbol"] for row in base_decisions
            if row.get("reversal_detected")
        }
        reversal_exposure = sum(
            safe_float(portfolio["positions"].get(symbol), 0.0)
            * safe_float(prices.get(symbol), 0.0)
            for symbol in reversal_symbols
        )
        reversal_sell_count = sum(
            1 for row in variant_orders
            if row.get("status") == "executed"
            and str(row.get("side") or "").lower() == "sell"
            and row.get("symbol") in reversal_symbols
        )
        cycle_rows.append({
            "timestamp": timestamp, "cycle_id": cycle_id, "strategy_name": name,
            "status": "ok", "config_hash": config_hash,
            "source_bar_timestamp": source_bar_timestamp,
            "production_equity": layer3_summary.get("equity"), "shadow_equity": round(equity, 2),
            "shadow_minus_production_equity": round(equity - safe_float(layer3_summary.get("equity"), 0.0), 2),
            "shadow_minus_control_equity": (
                round(equity - control_equity, 2) if control_equity is not None else 0.0
            ),
            "shadow_pnl_since_initialization": round(pnl, 2), "cash": round(portfolio["cash"], 2),
            "cash_pct": round(portfolio["cash"] / equity, 6) if equity else None,
            "peak_equity": round(portfolio["peak_equity"], 2), "drawdown_pct": round(drawdown, 8),
            "peak_giveback": round(equity - portfolio["peak_equity"], 2),
            "invested_pct": round(1.0 - portfolio["cash"] / equity, 6) if equity else None,
            "target_mode": config.get("target_mode", "control"),
            "signal_horizon_minutes": config.get("horizon_minutes"),
            "reversal_detected_count": len(reversal_symbols),
            "reversal_sell_count": reversal_sell_count,
            "reversal_exposure": round(reversal_exposure, 2),
            "reversal_exposure_pct": (
                round(reversal_exposure / equity, 8) if equity else None
            ),
            "cumulative_trade_count": portfolio["cumulative_trade_count"],
            "cumulative_gross_turnover": round(turnover, 2),
            "gross_turnover_pct": round(turnover / portfolio["initial_equity"], 8) if portfolio["initial_equity"] else None,
            "follow_up_trade_count": portfolio["follow_up_trade_count"],
            "direction_reversal_count": portfolio["direction_reversal_count"],
            "same_day_round_trip_symbol_count": sum(
                1 for sides in portfolio["traded_sides"].values()
                if {"BUY", "SELL"}.issubset(sides)
            ),
            "pnl_after_1bp_cost": round(pnl - turnover * 0.0001, 2),
            "pnl_after_5bp_cost": round(pnl - turnover * 0.0005, 2),
            "pnl_after_10bp_cost": round(pnl - turnover * 0.0010, 2),
            "pnl_after_20bp_cost": round(pnl - turnover * 0.0020, 2),
            "qualified_count": meta.get("qualified_count"), "selected_count": meta.get("selected_count"),
            "rejected_count": meta.get("rejected_count"), "deteriorating_count": meta.get("deteriorating_count"),
            "target_cash_pct": target.get("CASH"), "target_summary": target,
            "planner_status": planner.get("summary", {}).get("status"),
            "planner_decision_counts": planner.get("summary", {}).get("decision_counts"),
            "planner_rolling_trade_limits": planner.get("summary", {}).get("rolling_trade_limits"),
            "planner_target_hysteresis": planner.get("summary", {}).get("target_hysteresis"),
        })
        for base in ([] if name == "CURRENT_CONTROL" else base_decisions):
            decision_rows.append({
                "timestamp": timestamp, "cycle_id": cycle_id, "strategy_name": name,
                **base, "smoothed_target_weight": safe_float(target.get(base["symbol"]), 0.0),
            })
        for symbol in sorted(set(portfolio["positions"]) | set(target) - {"CASH", "_meta"}):
            qty = safe_float(portfolio["positions"].get(symbol), 0.0)
            price = safe_float(prices.get(symbol), 0.0)
            value = qty * price
            portfolio_rows.append({
                "timestamp": timestamp, "cycle_id": cycle_id, "strategy_name": name,
                "symbol": symbol, "qty": qty, "price": price, "market_value": value,
                "weight": value / equity if equity else 0.0,
                "target_weight": safe_float(target.get(symbol), 0.0),
                "cash": portfolio["cash"], "equity": equity,
            })

    append_layer_research_strategy_cycle_rows(cycle_rows)
    append_layer_research_strategy_decision_rows(decision_rows)
    append_layer_research_strategy_order_rows(order_rows)
    append_layer_research_strategy_portfolio_rows(portfolio_rows)
    state["last_result"] = cycle_rows
    logging.info(
        "[ResearchStrategyShadow] cycle=%s results=%s",
        cycle_id,
        {
            row["strategy_name"]: {
                "equity": row["shadow_equity"],
                "vs_control": row["shadow_minus_control_equity"],
                "cash_pct": row["cash_pct"],
                "turnover": row["cumulative_gross_turnover"],
                "drawdown": row["drawdown_pct"],
                "qualified": row["qualified_count"],
                "deteriorating": row["deteriorating_count"],
            }
            for row in cycle_rows
        },
    )
    return {"status": "ok", "cycles": cycle_rows}
