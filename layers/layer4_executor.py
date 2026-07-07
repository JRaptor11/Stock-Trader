# layers/layer4_executor.py

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from core.state import app_state
from layers.layer_csv import append_layer4_shadow_rows
from layers.layer5_executor import execute_layer5_plan
from utils.numeric import safe_float
from utils.symbols import normalize_symbol




def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _clamp01(value: float) -> float:
    return _clamp(value, 0.0, 1.0)


def _normalize_decision(value: Any) -> str:
    return str(value or "").upper().strip()


def _get_plan_rows(plan: Any) -> list[dict]:
    if isinstance(plan, list):
        return [row for row in plan if isinstance(row, dict)]

    if isinstance(plan, dict):
        for key in ("rows", "plan", "decisions"):
            rows = plan.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]

    return []


def _extract_order_values(row: dict) -> dict:
    symbol = normalize_symbol(row.get("symbol"))
    decision = _normalize_decision(row.get("decision"))

    qty = safe_float(
        row.get(
            "remaining_authorized_qty",
            row.get(
                "max_authorized_qty",
                row.get("planned_qty", row.get("qty", 0.0)),
            ),
        ),
        0.0,
    )

    price = safe_float(row.get("live_price", row.get("price", 0.0)), 0.0)

    notional = safe_float(
        row.get(
            "remaining_authorized_notional",
            row.get(
                "max_authorized_notional",
                row.get("planned_notional", row.get("notional", 0.0)),
            ),
        ),
        0.0,
    )

    if notional <= 0 and qty > 0 and price > 0:
        notional = qty * price

    return {
        "symbol": symbol,
        "decision": decision,
        "qty": qty,
        "price": price,
        "notional": notional,
    }


def _executable_rows(plan: Any) -> list[dict]:
    rows = []

    for row in _get_plan_rows(plan):
        values = _extract_order_values(row)

        if values["decision"] not in {"BUY", "SELL"}:
            continue

        if not values["symbol"]:
            continue

        if values["qty"] <= 0:
            continue

        if values["price"] <= 0:
            continue

        rows.append({**row, **values})

    rows.sort(
        key=lambda r: (
            0 if r["decision"] == "SELL" else 1,
            -abs(safe_float(r.get("notional"), 0.0)),
        )
    )

    return rows


def _pct_change_from_points(points: list[tuple[float, float]], seconds: int) -> float | None:
    if len(points) < 2:
        return None

    last_ts, last_price = points[-1]
    cutoff = float(last_ts) - float(seconds)

    anchor_price = None

    for ts, price in reversed(points):
        if float(ts) <= cutoff:
            anchor_price = float(price)
            break

    if anchor_price is None:
        anchor_price = float(points[0][1])

    if anchor_price <= 0:
        return None

    return (float(last_price) - anchor_price) / anchor_price


def _range_component(price: float, bars: list[dict]) -> float:
    highs = [safe_float(bar.get("high"), 0.0) for bar in bars]
    lows = [safe_float(bar.get("low"), 0.0) for bar in bars]

    highs = [value for value in highs if value > 0]
    lows = [value for value in lows if value > 0]

    if not highs or not lows:
        return 0.0

    high = max(highs)
    low = min(lows)

    if high <= low:
        return 0.0

    # -1 means near live range low, +1 means near live range high.
    return _clamp(((price - low) / (high - low) * 2.0) - 1.0, -1.0, 1.0)


def _volume_component(bars_1m: list[dict]) -> float:
    if len(bars_1m) < 3:
        return 0.0

    current_volume = safe_float(bars_1m[-1].get("volume"), 0.0)
    prior = [
        safe_float(bar.get("volume"), 0.0)
        for bar in bars_1m[-6:-1]
    ]
    prior = [value for value in prior if value > 0]

    if current_volume <= 0 or not prior:
        return 0.0

    avg_prior = sum(prior) / len(prior)

    if avg_prior <= 0:
        return 0.0

    ratio = current_volume / avg_prior

    # ratio 1.0 = neutral, ratio 3.0 = max positive.
    return _clamp((ratio - 1.0) / 2.0, -1.0, 1.0)


def _live_metrics(symbol: str, row_price: float) -> dict:
    md = app_state.get("market_data", {}).get("buffer")

    if md is None:
        return {
            "has_live_data": False,
            "live_tick_count": 0,
            "live_1m_bar_count": 0,
            "live_5m_bar_count": 0,
            "pressure": 0.0,
            "buy_chase_risk": 0.0,
            "ret_30s": None,
            "ret_60s": None,
            "ret_300s": None,
            "range_component": 0.0,
            "volume_component": 0.0,
        }

    points = list(md.get_recent_prices_ts(symbol, limit=240) or [])

    if hasattr(md, "get_live_bars"):
        bars_1m = list(md.get_live_bars(symbol, timeframe_seconds=60, limit=10) or [])
        bars_5m = list(md.get_live_bars(symbol, timeframe_seconds=300, limit=6) or [])
    else:
        bars_1m = []
        bars_5m = []

    live_price = safe_float(points[-1][1], row_price) if points else row_price
    price = live_price if live_price > 0 else row_price

    ret_30s = _pct_change_from_points(points, 30)
    ret_60s = _pct_change_from_points(points, 60)
    ret_300s = _pct_change_from_points(points, 300)

    r30 = ret_30s if ret_30s is not None else 0.0
    r60 = ret_60s if ret_60s is not None else 0.0
    r300 = ret_300s if ret_300s is not None else 0.0

    range_bars = bars_1m[-5:] if bars_1m else bars_5m[-1:]
    range_score = _range_component(price, range_bars)
    volume_score = _volume_component(bars_1m)

    live_pressure_score = _clamp(
        (35.0 * r60)
        + (18.0 * r300)
        + (0.30 * range_score)
        + (0.15 * volume_score),
        -1.0,
        1.0,
    )

    buy_chase_risk = _clamp01(
        (_clamp(max(0.0, r30) / 0.0030, 0.0, 1.0) * 0.25)
        + (_clamp(max(0.0, r60) / 0.0045, 0.0, 1.0) * 0.35)
        + (_clamp(max(0.0, r300) / 0.0120, 0.0, 1.0) * 0.25)
        + (_clamp(max(0.0, range_score), 0.0, 1.0) * 0.15)
    )

    return {
        "has_live_data": len(points) >= 3,
        "live_tick_count": len(points),
        "live_1m_bar_count": len(bars_1m),
        "live_5m_bar_count": len(bars_5m),
        "live_price": round(price, 4) if price > 0 else None,
        "row_price": round(row_price, 4) if row_price > 0 else None,
        "live_vs_row_price_pct": (
            round((price - row_price) / row_price, 6)
            if row_price > 0 and price > 0
            else None
        ),
        "latest_1m_close": (
            round(safe_float(bars_1m[-1].get("close"), 0.0), 4)
            if bars_1m else None
        ),
        "latest_5m_close": (
            round(safe_float(bars_5m[-1].get("close"), 0.0), 4)
            if bars_5m else None
        ),
        "latest_1m_volume": (
            round(safe_float(bars_1m[-1].get("volume"), 0.0), 2)
            if bars_1m else None
        ),
        "latest_5m_volume": (
            round(safe_float(bars_5m[-1].get("volume"), 0.0), 2)
            if bars_5m else None
        ),
        "pressure": round(live_pressure_score, 4),
        "buy_chase_risk": round(buy_chase_risk, 4),
        "ret_30s": round(ret_30s, 6) if ret_30s is not None else None,
        "ret_60s": round(ret_60s, 6) if ret_60s is not None else None,
        "ret_300s": round(ret_300s, 6) if ret_300s is not None else None,
        "range_component": round(range_score, 4),
        "volume_component": round(volume_score, 4),
    }


def _position_green_metrics(symbol: str, price: float) -> tuple[bool, float | None]:
    trade_info = app_state.get("open_trades", {}).get(symbol)

    if not isinstance(trade_info, dict):
        return False, None

    entry = safe_float(
        trade_info.get("buy_price", trade_info.get("avg_entry_price", 0.0)),
        0.0,
    )

    if entry <= 0 or price <= 0:
        return False, None

    unrealized_plpc = (price - entry) / entry

    return unrealized_plpc > 0, round(unrealized_plpc, 6)


def _sell_classification(row: dict) -> str:
    reason = str(row.get("reason") or "").lower()
    target_weight = safe_float(row.get("target_weight"), 0.0)
    current_qty = safe_float(row.get("current_qty"), 0.0)

    if "risk" in reason or "fail_safe" in reason or "loss" in reason:
        return "risk_sell"

    if target_weight <= 0 and current_qty > 0:
        return "target_removed_exit"

    if "target_removed" in reason:
        return "target_removed_exit"

    if "overweight" in reason or "trim" in reason or "scale_out" in reason:
        return "overweight_trim"

    return "sell"


def _score_buy(metrics: dict) -> dict:
    pressure = safe_float(metrics.get("pressure"), 0.0)
    chase = safe_float(metrics.get("buy_chase_risk"), 0.0)

    if not metrics.get("has_live_data"):
        return {
            "shadow_action": "execute_neutral_no_live_data",
            "shadow_confidence": 0.15,
            "shadow_reason": "insufficient_live_ticks_neutral",
            "would_execute": True,
            "would_delay": False,
            "would_reduce": False,
            "would_block": False,
            "recommended_qty_multiplier": 1.0,
        }

    if pressure <= -0.20:
        return {
            "shadow_action": "delay_buy_no_live_confirmation",
            "shadow_confidence": round(abs(pressure), 4),
            "shadow_reason": f"buy_pressure_negative:{pressure:.2f}",
            "would_execute": False,
            "would_delay": True,
            "would_reduce": False,
            "would_block": False,
            "recommended_qty_multiplier": 0.0,
        }

    if chase >= 0.78:
        return {
            "shadow_action": "block_buy_chase",
            "shadow_confidence": round(chase, 4),
            "shadow_reason": f"extended_live_move_chase_risk:{chase:.2f}",
            "would_execute": False,
            "would_delay": False,
            "would_reduce": False,
            "would_block": True,
            "recommended_qty_multiplier": 0.0,
        }

    if chase >= 0.55:
        return {
            "shadow_action": "reduce_buy_chase_size",
            "shadow_confidence": round(chase, 4),
            "shadow_reason": f"moderate_chase_risk:{chase:.2f};pressure:{pressure:.2f}",
            "would_execute": True,
            "would_delay": False,
            "would_reduce": True,
            "would_block": False,
            "recommended_qty_multiplier": 0.50,
        }

    if pressure >= 0.20:
        return {
            "shadow_action": "execute_buy_confirmed",
            "shadow_confidence": round(max(pressure, 1.0 - chase), 4),
            "shadow_reason": f"live_pressure_confirms_buy:{pressure:.2f};chase:{chase:.2f}",
            "would_execute": True,
            "would_delay": False,
            "would_reduce": False,
            "would_block": False,
            "recommended_qty_multiplier": 1.0,
        }

    return {
        "shadow_action": "delay_buy_weak_confirmation",
        "shadow_confidence": round(max(0.05, 0.20 - pressure), 4),
        "shadow_reason": f"live_pressure_not_strong_enough:{pressure:.2f}",
        "would_execute": False,
        "would_delay": True,
        "would_reduce": False,
        "would_block": False,
        "recommended_qty_multiplier": 0.0,
    }


def _score_sell(row: dict, metrics: dict, position_green: bool, unrealized_plpc: float | None) -> dict:
    pressure = safe_float(metrics.get("pressure"), 0.0)
    classification = _sell_classification(row)
    plpc = safe_float(unrealized_plpc, 0.0) if unrealized_plpc is not None else 0.0

    sell_strength_protection = _clamp01(
        (_clamp(max(0.0, pressure), 0.0, 1.0) * 0.55)
        + (_clamp(max(0.0, plpc) / 0.025, 0.0, 1.0) * 0.35)
        + (0.10 if position_green else 0.0)
    )

    if classification == "risk_sell":
        return {
            "sell_classification": classification,
            "sell_strength_protection": round(sell_strength_protection, 4),
            "shadow_action": "execute_risk_sell",
            "shadow_confidence": 0.95,
            "shadow_reason": "risk_sell_overrides_live_strength_protection",
            "would_execute": True,
            "would_delay": False,
            "would_reduce": False,
            "would_block": False,
            "recommended_qty_multiplier": 1.0,
        }

    if metrics.get("has_live_data") and sell_strength_protection >= 0.72:
        return {
            "sell_classification": classification,
            "sell_strength_protection": round(sell_strength_protection, 4),
            "shadow_action": "delay_sell_protect_winner",
            "shadow_confidence": round(sell_strength_protection, 4),
            "shadow_reason": (
                f"position_green_or_live_strength:{sell_strength_protection:.2f};"
                f"pressure:{pressure:.2f}"
            ),
            "would_execute": False,
            "would_delay": True,
            "would_reduce": False,
            "would_block": False,
            "recommended_qty_multiplier": 0.0,
        }

    if metrics.get("has_live_data") and sell_strength_protection >= 0.48:
        return {
            "sell_classification": classification,
            "sell_strength_protection": round(sell_strength_protection, 4),
            "shadow_action": "reduce_sell_size_protect_strength",
            "shadow_confidence": round(sell_strength_protection, 4),
            "shadow_reason": (
                f"some_live_strength_or_green_position:{sell_strength_protection:.2f};"
                f"classification:{classification}"
            ),
            "would_execute": True,
            "would_delay": False,
            "would_reduce": True,
            "would_block": False,
            "recommended_qty_multiplier": 0.50,
        }

    return {
        "sell_classification": classification,
        "sell_strength_protection": round(sell_strength_protection, 4),
        "shadow_action": "execute_sell_unprotected",
        "shadow_confidence": round(max(0.20, 1.0 - sell_strength_protection), 4),
        "shadow_reason": (
            f"no_strong_live_winner_protection;"
            f"classification:{classification};"
            f"protection:{sell_strength_protection:.2f}"
        ),
        "would_execute": True,
        "would_delay": False,
        "would_reduce": False,
        "would_block": False,
        "recommended_qty_multiplier": 1.0,
    }


def score_layer4_shadow_for_row(row: dict) -> dict:
    values = _extract_order_values(row)

    symbol = values["symbol"]
    decision = values["decision"]
    price = values["price"]

    metrics = _live_metrics(symbol, price)
    position_green, unrealized_plpc = _position_green_metrics(symbol, price)

    if decision == "BUY":
        decision_result = _score_buy(metrics)
        sell_classification = ""
        sell_strength_protection = 0.0

    elif decision == "SELL":
        decision_result = _score_sell(row, metrics, position_green, unrealized_plpc)
        sell_classification = decision_result.pop("sell_classification", "sell")
        sell_strength_protection = decision_result.pop("sell_strength_protection", 0.0)

    else:
        decision_result = {
            "shadow_action": "ignore_non_executable",
            "shadow_confidence": 0.0,
            "shadow_reason": "non_buy_sell_row",
            "would_execute": False,
            "would_delay": False,
            "would_reduce": False,
            "would_block": False,
            "recommended_qty_multiplier": 0.0,
        }
        sell_classification = ""
        sell_strength_protection = 0.0

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "row_id": row.get("row_id"),
        "symbol": symbol,
        "side": decision.lower(),
        "decision": decision,
        "layer3_reason": row.get("reason"),

        "qty": values["qty"],
        "notional": values["notional"],
        "price": price,

        "current_qty": row.get("current_qty"),
        "target_qty": row.get("target_qty"),
        "current_weight": row.get("current_weight"),
        "target_weight": row.get("target_weight"),
        "delta_weight": row.get("delta_weight"),
        "relative_drift": row.get("relative_drift"),

        "live_tick_count": metrics.get("live_tick_count"),
        "live_1m_bar_count": metrics.get("live_1m_bar_count"),
        "live_5m_bar_count": metrics.get("live_5m_bar_count"),
        "live_price": metrics.get("live_price"),
        "row_price": metrics.get("row_price"),
        "live_vs_row_price_pct": metrics.get("live_vs_row_price_pct"),
        "latest_1m_close": metrics.get("latest_1m_close"),
        "latest_5m_close": metrics.get("latest_5m_close"),
        "latest_1m_volume": metrics.get("latest_1m_volume"),
        "latest_5m_volume": metrics.get("latest_5m_volume"),
        "live_ret_30s": metrics.get("ret_30s"),
        "live_ret_60s": metrics.get("ret_60s"),
        "live_ret_300s": metrics.get("ret_300s"),
        "live_range_component": metrics.get("range_component"),
        "live_volume_component": metrics.get("volume_component"),

        "live_pressure_score": metrics.get("pressure"),
        "buy_chase_risk": metrics.get("buy_chase_risk"),
        "sell_strength_protection": sell_strength_protection,
        "sell_classification": sell_classification,
        "position_green": position_green,
        "position_unrealized_plpc": unrealized_plpc,

        **decision_result,
    }


def score_layer4_shadow_for_plan(plan: Any, summary: dict | None = None) -> dict:
    """
    Diagnostic-only Layer 4 tactical scoring.

    This function must not submit, resize, cancel, delay, or otherwise alter
    live orders. It only scores Layer 3 executable rows and writes shadow CSV.
    """

    started = time.monotonic()
    summary = summary or {}

    cycle_id = summary.get("cycle_id")
    plan_id = summary.get("plan_id")

    result = {
        "layer": "layer4",
        "mode": "shadow_only",
        "cycle_id": cycle_id,
        "plan_id": plan_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "duration_seconds": None,
        "row_count": 0,
        "execute_count": 0,
        "delay_count": 0,
        "reduce_count": 0,
        "block_count": 0,
        "rows": [],
        "error": None,
    }

    try:
        rows = []

        for row in _executable_rows(plan):
            scored = score_layer4_shadow_for_row(row)
            scored["cycle_id"] = row.get("cycle_id") or cycle_id
            scored["plan_id"] = row.get("plan_id") or plan_id
            rows.append(scored)

        result["rows"] = rows
        result["row_count"] = len(rows)
        result["execute_count"] = sum(1 for row in rows if row.get("would_execute"))
        result["delay_count"] = sum(1 for row in rows if row.get("would_delay"))
        result["reduce_count"] = sum(1 for row in rows if row.get("would_reduce"))
        result["block_count"] = sum(1 for row in rows if row.get("would_block"))

    except Exception as exc:
        result["error"] = str(exc)
        logging.exception(
            "[Layer4Shadow] Failed scoring shadow plan | cycle_id=%s plan_id=%s",
            cycle_id,
            plan_id,
        )

    finally:
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["duration_seconds"] = round(time.monotonic() - started, 3)

        try:
            append_layer4_shadow_rows(result)
        except Exception:
            logging.warning("[Layer4Shadow] Failed appending shadow CSV rows.", exc_info=True)

        layers = app_state.setdefault("layers", {})
        shadow_state = layers.setdefault("layer4_shadow", {})
        shadow_state["last_cycle_id"] = cycle_id
        shadow_state["last_plan_id"] = plan_id
        shadow_state["last_result"] = result
        shadow_state["last_run_at"] = result["finished_at"]

        logging.info(
            "[Layer4Shadow] Complete | cycle_id=%s plan_id=%s rows=%s "
            "execute=%s delay=%s reduce=%s block=%s error=%s",
            cycle_id,
            plan_id,
            result.get("row_count"),
            result.get("execute_count"),
            result.get("delay_count"),
            result.get("reduce_count"),
            result.get("block_count"),
            result.get("error"),
        )

    return result


def _empty_shadow_result(cycle_id, plan_id, error: str | None = None) -> dict:
    return {
        "layer": "layer4",
        "mode": "shadow_only",
        "cycle_id": cycle_id,
        "plan_id": plan_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": 0.0,
        "row_count": 0,
        "execute_count": 0,
        "delay_count": 0,
        "reduce_count": 0,
        "block_count": 0,
        "rows": [],
        "error": error,
    }


def execute_layer4_plan(plan: Any, summary: dict | None = None) -> dict:
    """
    Layer 4 tactical wrapper.

    Current phase:
    - Layer 4 runs tactical live scoring in shadow mode only.
    - Layer 4 does NOT block, resize, delay, or alter orders.
    - Actual broker execution is delegated directly to Layer 5.

    Compatibility:
    - The function name stays execute_layer4_plan() so layer_monitor.py does
      not need to change yet.
    """

    started = time.monotonic()
    summary = summary or {}

    cycle_id = summary.get("cycle_id")
    plan_id = summary.get("plan_id")

    layers = app_state.setdefault("layers", {})
    layer4_state = layers.setdefault("layer4", {})

    layer4_state["last_attempted_at"] = datetime.now(timezone.utc).isoformat()
    layer4_state["last_cycle_id"] = cycle_id
    layer4_state["last_plan_id"] = plan_id
    layer4_state["mode"] = "shadow_only_delegating_to_layer5"

    try:
        shadow_result = score_layer4_shadow_for_plan(plan, summary)
    except Exception as exc:
        logging.warning(
            "[Layer4Shadow] Shadow scoring failed; continuing to Layer 5 execution. "
            "cycle_id=%s plan_id=%s error=%s",
            cycle_id,
            plan_id,
            exc,
            exc_info=True,
        )
        shadow_result = _empty_shadow_result(cycle_id, plan_id, error=str(exc))

    # Important: actual execution remains unchanged and is delegated to Layer 5.
    execution_result = execute_layer5_plan(plan, summary)

    if not isinstance(execution_result, dict):
        execution_result = {
            "layer": "layer5",
            "mode": "direct_compat_execution",
            "cycle_id": cycle_id,
            "plan_id": plan_id,
            "blocked_reason": "layer5_returned_non_dict_result",
            "orders": [],
        }

    execution_result["shadow_result"] = shadow_result
    execution_result["layer4_mode"] = "shadow_only"
    execution_result["execution_layer"] = "layer5"

    layer4_state["last_result"] = {
        "layer": "layer4",
        "mode": "shadow_only_delegating_to_layer5",
        "cycle_id": cycle_id,
        "plan_id": plan_id,
        "started_at": shadow_result.get("started_at"),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "shadow_result": shadow_result,
        "delegated_execution_layer": "layer5",
        "layer5_blocked_reason": execution_result.get("blocked_reason"),
        "layer5_attempted": execution_result.get("attempted"),
        "layer5_submitted": execution_result.get("submitted"),
        "layer5_skipped": execution_result.get("skipped"),
        "layer5_errors": execution_result.get("errors"),
    }

    logging.info(
        "[Layer4] Shadow complete; delegated execution to Layer 5 | "
        "cycle_id=%s plan_id=%s shadow_rows=%s shadow_delay=%s "
        "shadow_reduce=%s shadow_block=%s layer5_attempted=%s layer5_submitted=%s",
        cycle_id,
        plan_id,
        shadow_result.get("row_count"),
        shadow_result.get("delay_count"),
        shadow_result.get("reduce_count"),
        shadow_result.get("block_count"),
        execution_result.get("attempted"),
        execution_result.get("submitted"),
    )

    return execution_result