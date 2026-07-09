# runners/layer_monitor.py
import asyncio
import logging
import math
import time
from datetime import datetime, timezone, timedelta

from core.market_clock import get_market_is_open
from layers.layer_logging import target_summary_for_log
from layers.layer_schedule import (
    normalize_layer_interval_seconds,
    sleep_until_next_layer_boundary,
)
from utils.numeric import safe_float, safe_int

from config import runtime_config as config
from market.bar_data import (
    fetch_recent_bars_with_min_count,
    filter_fresh_bars,
)

from core.state import app_state
from layers.layer1_ranker import Layer1StockRanker
from layers.layer2_portfolio import Layer2PortfolioBuilder
from layers.layer3_rebalancer import run_layer3_dry_run
from layers.layer4_executor import execute_layer4_plan
from layers.layer_csv import (
    append_layer_cycle_row,
    append_layer_live_bar_health_rows,
    append_layer_live_strategy_outcome_rows,
    append_layer_live_strategy_shadow_cycle_row,
    append_layer_live_strategy_shadow_rows,
)


def _execution_setting(name: str, default):
    return app_state.get("execution", {}).get(
        name,
        getattr(config, name.upper(), default),
    )


def _expire_active_plan_for_market_close() -> None:
    layers = app_state.setdefault("layers", {})
    active_plan = layers.get("active_execution_plan")

    if isinstance(active_plan, dict) and active_plan.get("status") in {"active", "working"}:
        now = datetime.now(timezone.utc).isoformat()
        active_plan["status"] = "expired_market_closed"
        active_plan["expired_at"] = now
        active_plan["expired_reason"] = "market_closed"

        history = layers.setdefault("execution_plan_history", [])
        history.append(active_plan)
        del history[:-10]

        layers["active_execution_plan"] = None

        layer4 = layers.setdefault("layer4", {})
        layer4["active_plan_id"] = None
        layer4["active_plan_expires_at"] = None

        logging.info(
            "[Layers] Expired active execution plan because market is closed | plan_id=%s",
            active_plan.get("plan_id"),
        )


def _bar_value(bar, key: str, default=None):
    if isinstance(bar, dict):
        return bar.get(key, default)
    return getattr(bar, key, default)


def _latest_rest_bar_timestamp(bar) -> datetime | None:
    raw = _bar_value(bar, "timestamp") or _bar_value(bar, "t")

    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)

    if raw is None:
        return None

    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _latest_close_from_bar(bar) -> float | None:
    value = _bar_value(bar, "close")
    close = safe_float(value, 0.0)
    return close if close > 0 else None


def _build_live_bar_health_rows(
    *,
    symbols: list[str],
    rest_bars_by_symbol: dict,
    market_is_open: bool,
    cycle_id=None,
) -> list[dict]:
    md = app_state.get("market_data", {}).get("buffer")

    if md is None:
        return []

    rows = []
    now = datetime.now(timezone.utc)

    for symbol in symbols:
        try:
            recent_prices = list(md.get_recent_prices(symbol) or [])
            bars_1m = list(md.get_live_bars(symbol, timeframe_seconds=60, limit=10) or []) if hasattr(md, "get_live_bars") else []
            bars_5m = list(md.get_live_bars(symbol, timeframe_seconds=300, limit=6) or []) if hasattr(md, "get_live_bars") else []

            latest_live_price = safe_float(recent_prices[-1], 0.0) if recent_prices else 0.0
            latest_1m = bars_1m[-1] if bars_1m else None
            latest_5m = bars_5m[-1] if bars_5m else None

            rest_bars = list(rest_bars_by_symbol.get(symbol, []) or [])
            latest_rest = rest_bars[-1] if rest_bars else None
            rest_close = _latest_close_from_bar(latest_rest) if latest_rest is not None else None
            rest_ts = _latest_rest_bar_timestamp(latest_rest) if latest_rest is not None else None

            rest_age_minutes = None
            if rest_ts is not None:
                rest_age_minutes = round(max(0.0, (now - rest_ts).total_seconds() / 60.0), 3)

            live_vs_rest_close_pct = None
            if latest_live_price > 0 and rest_close and rest_close > 0:
                live_vs_rest_close_pct = round((latest_live_price - rest_close) / rest_close, 6)

            rows.append({
                "timestamp": now.isoformat(),
                "cycle_id": cycle_id,
                "symbol": symbol,
                "market_is_open": market_is_open,
                "tick_count": len(recent_prices),
                "live_1m_bar_count": len(bars_1m),
                "live_5m_bar_count": len(bars_5m),
                "latest_live_price": round(latest_live_price, 4) if latest_live_price > 0 else None,
                "latest_1m_close": round(safe_float(_bar_value(latest_1m, "close"), 0.0), 4) if latest_1m else None,
                "latest_5m_close": round(safe_float(_bar_value(latest_5m, "close"), 0.0), 4) if latest_5m else None,
                "latest_1m_volume": round(safe_float(_bar_value(latest_1m, "volume"), 0.0), 2) if latest_1m else None,
                "latest_5m_volume": round(safe_float(_bar_value(latest_5m, "volume"), 0.0), 2) if latest_5m else None,
                "rest_bar_count": len(rest_bars),
                "rest_latest_close": round(rest_close, 4) if rest_close else None,
                "rest_latest_timestamp": rest_ts.isoformat() if rest_ts else None,
                "rest_bar_age_minutes": rest_age_minutes,
                "live_vs_rest_close_pct": live_vs_rest_close_pct,
            })

        except Exception:
            logging.warning(
                "[Layers] Failed building live-bar health row for %s.",
                symbol,
                exc_info=True,
            )

    return rows


def _append_live_bar_health_snapshot(
    *,
    symbols: list[str],
    rest_bars_by_symbol: dict,
    market_is_open: bool,
    cycle_id=None,
) -> None:
    rows = _build_live_bar_health_rows(
        symbols=symbols,
        rest_bars_by_symbol=rest_bars_by_symbol,
        market_is_open=market_is_open,
        cycle_id=cycle_id,
    )

    if not rows:
        return

    append_layer_live_bar_health_rows(rows)

    compact = {
        row["symbol"]: {
            "ticks": row.get("tick_count"),
            "1m": row.get("live_1m_bar_count"),
            "5m": row.get("live_5m_bar_count"),
            "live_vs_rest": row.get("live_vs_rest_close_pct"),
        }
        for row in rows
    }

    logging.info("[Layers] Live bar health snapshot: %s", compact)


def _fresh_symbol_requirement(symbol_count: int) -> int:
    min_symbols = safe_int(
        _execution_setting("bar_freshness_min_fresh_symbols", 5),
        5,
    )

    min_ratio = safe_float(
        _execution_setting("bar_freshness_min_fresh_ratio", 0.70),
        0.70,
    )

    ratio_required = math.ceil(symbol_count * min_ratio)

    return max(1, min(symbol_count, max(min_symbols, ratio_required)))


def _layer2_evaluation_context(market_is_open: bool, *, count_live_cycle: bool) -> dict:
    """
    Build context passed into Layer 2 smoothing.

    We count only executable market-open Layer 1/2 evaluations, not skipped
    freshness cycles and not closed-market warmups. This lets Layer 2 damp
    large target shocks for the first few real live cycles after the open.
    """
    layers = app_state.setdefault("layers", {})
    state = layers.setdefault("opening_transition", {})

    today = datetime.now(timezone.utc).date().isoformat()

    if state.get("date") != today:
        state.clear()
        state["date"] = today
        state["live_evaluation_count"] = 0

    transition_cycles = safe_int(
        _execution_setting("layer2_opening_transition_smoothing_cycles", 3),
        3,
    )

    if market_is_open and count_live_cycle:
        state["live_evaluation_count"] = safe_int(
            state.get("live_evaluation_count", 0),
            0,
        ) + 1

    live_cycle = safe_int(state.get("live_evaluation_count", 0), 0)
    active = bool(market_is_open and live_cycle > 0 and live_cycle <= transition_cycles)

    state["transition_cycles"] = transition_cycles
    state["active"] = active
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    return {
        "market_is_open": bool(market_is_open),
        "opening_transition_active": active,
        "opening_transition_cycle": live_cycle if market_is_open else None,
        "opening_transition_cycles": transition_cycles,
    }


def store_latest_layer_result(symbols, bar_counts, ranked, target):
    """
    Store the latest Layer 1/2 result in app_state so Layer 3 can read it.

    This function does not place trades.
    It only creates the handoff from Layer 1/2 target generation
    to Layer 3 planning/execution.
    """
    layers = app_state.setdefault("layers", {})

    ranked_snapshot = []
    for r in ranked or []:
        ranked_snapshot.append({
            "symbol": getattr(r, "symbol", None),
            "score": float(getattr(r, "score", 0.0) or 0.0),
            "last_price": float(getattr(r, "last_price", 0.0) or 0.0),
            "reason": getattr(r, "reason", ""),
        })

    target = target or {}
    target_meta = target.get("_meta", {}) if isinstance(target, dict) else {}

    latest = layers.setdefault("latest", {})
    latest["timestamp"] = datetime.now(timezone.utc).isoformat()
    latest["symbols_evaluated"] = list(symbols or [])
    latest["bar_counts"] = dict(bar_counts or {})
    latest["ranked"] = ranked_snapshot
    latest["target_portfolio"] = dict(target)
    latest["target_meta"] = dict(target_meta)

    rebalance = layers.setdefault("rebalance", {})
    rebalance.setdefault("enabled", True)
    rebalance.setdefault("dry_run", True)
    rebalance.setdefault("last_cycle_id", 0)
    rebalance.setdefault("last_run_at", None)
    rebalance.setdefault("last_plan", [])
    rebalance.setdefault("last_summary", {})
    rebalance.setdefault("target_seen_counts", {})
    rebalance.setdefault("target_absent_counts", {})
    rebalance.setdefault("last_error", None)

    rebalance.setdefault("bootstrap_confirmation_applied", False)
    rebalance.setdefault("bootstrap_confirmation_symbols", [])
    rebalance.setdefault("last_confirmation_update_at", None)
    rebalance.setdefault("confirmation_updates_allowed", None)
    rebalance.setdefault("confirmation_updates_blocked_reason", None)

    logging.info(
        "[Layers] Stored latest Layer 1/2 result for Layer 3 | ranked_count=%s target_summary=%s",
        len(ranked_snapshot),
        target_summary_for_log(target),
    )


def store_off_hours_layer_warmup_result(symbols, bar_counts, ranked, target, freshness_report):
    """
    Store a non-executable Layer 1/2 warmup target while the market is closed.

    This intentionally does not update app_state["layers"]["latest"], because
    that object is the Layer 3 handoff used for executable planning. The warmup
    still matters because layer_engine.evaluate() updates Layer 2's internal
    previous_target_portfolio, so the first market-open target can be smoothed
    against an existing target instead of becoming a brand-new first_target.
    """
    layers = app_state.setdefault("layers", {})

    ranked_snapshot = []
    for r in ranked or []:
        ranked_snapshot.append({
            "symbol": getattr(r, "symbol", None),
            "score": float(getattr(r, "score", 0.0) or 0.0),
            "last_price": float(getattr(r, "last_price", 0.0) or 0.0),
            "reason": getattr(r, "reason", ""),
        })

    target = target or {}
    target_meta = target.get("_meta", {}) if isinstance(target, dict) else {}

    layers["last_off_hours_warmup"] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": "market_closed_target_warmup",
        "executable": False,
        "symbols_evaluated": list(symbols or []),
        "bar_counts": dict(bar_counts or {}),
        "ranked": ranked_snapshot,
        "target_portfolio": dict(target),
        "target_meta": dict(target_meta),
        "freshness_report": dict(freshness_report or {}),
    }

    logging.info(
        "[Layers] Stored off-hours Layer 1/2 warmup target | ranked_count=%s target_summary=%s",
        len(ranked_snapshot),
        target_summary_for_log(target),
    )


# ================================================================
# LIVE-BAR LAYER 1/2 SHADOW COMPARISON
# ---------------------------------------------------------------
# This does NOT trade.
# It exists only to compare:
#   current REST/delayed-bar Layer 1/2 target vs live-bar Layer 1/2 target
# ================================================================


def _target_meta(target: dict | None) -> dict:
    if not isinstance(target, dict):
        return {}
    meta = target.get("_meta", {})
    return meta if isinstance(meta, dict) else {}


def _target_weight(target: dict | None, symbol: str) -> float:
    if not isinstance(target, dict):
        return 0.0
    return safe_float(target.get(symbol), 0.0)


def _target_symbols(target: dict | None) -> set[str]:
    if not isinstance(target, dict):
        return set()

    out = set()
    for raw_symbol, raw_weight in target.items():
        symbol = str(raw_symbol or "").upper().strip()
        if not symbol or symbol in {"CASH", "USD", "_META"} or symbol.startswith("_"):
            continue
        if safe_float(raw_weight, 0.0) > 0:
            out.add(symbol)
    return out


def _rank_snapshot(ranked: list | None) -> tuple[dict, list[str]]:
    rank_map = {}
    top_symbols = []

    for index, row in enumerate(ranked or [], start=1):
        symbol = str(getattr(row, "symbol", "") or "").upper().strip()
        if not symbol:
            continue

        if len(top_symbols) < 5:
            top_symbols.append(symbol)

        rank_map[symbol] = {
            "rank": index,
            "score": safe_float(getattr(row, "score", 0.0), 0.0),
            "last_price": safe_float(getattr(row, "last_price", 0.0), 0.0),
            "reason": getattr(row, "reason", ""),
        }

    return rank_map, top_symbols


def _latest_live_price_for_symbol(md, symbol: str, live_bars: list[dict] | None = None) -> float:
    try:
        recent_prices = list(md.get_recent_prices(symbol, limit=1) or []) if md is not None else []
        if recent_prices:
            price = safe_float(recent_prices[-1], 0.0)
            if price > 0:
                return price
    except Exception:
        pass

    try:
        if live_bars:
            price = safe_float(live_bars[-1].get("close"), 0.0)
            if price > 0:
                return price
    except Exception:
        pass

    return 0.0


def _build_live_bars_by_symbol(
    symbols: list[str],
    *,
    timeframe_seconds: int,
    limit: int,
) -> tuple[dict, dict, dict]:
    md = app_state.get("market_data", {}).get("buffer")
    if md is None or not hasattr(md, "get_live_bars"):
        return {}, {}, {}

    bars_by_symbol = {}
    bar_counts = {}
    live_prices = {}

    for symbol in symbols or []:
        symbol = str(symbol or "").upper().strip()
        if not symbol:
            continue

        try:
            bars = list(
                md.get_live_bars(
                    symbol,
                    timeframe_seconds=timeframe_seconds,
                    limit=limit,
                ) or []
            )
        except Exception:
            logging.warning(
                "[LiveStrategyShadow] Failed reading live bars for %s.",
                symbol,
                exc_info=True,
            )
            bars = []

        bars_by_symbol[symbol] = bars
        bar_counts[symbol] = len(bars)
        live_prices[symbol] = _latest_live_price_for_symbol(md, symbol, bars)

    return bars_by_symbol, bar_counts, live_prices


def _live_shadow_evaluation_context(market_is_open: bool, *, count_live_cycle: bool) -> dict:
    layers = app_state.setdefault("layers", {})
    shadow = layers.setdefault("live_strategy_shadow", {})
    state = shadow.setdefault("opening_transition", {})

    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("date") != today:
        state.clear()
        state["date"] = today
        state["live_evaluation_count"] = 0

    transition_cycles = safe_int(
        _execution_setting("layer2_opening_transition_smoothing_cycles", 3),
        3,
    )

    if market_is_open and count_live_cycle:
        state["live_evaluation_count"] = safe_int(
            state.get("live_evaluation_count", 0),
            0,
        ) + 1

    live_cycle = safe_int(state.get("live_evaluation_count", 0), 0)
    active = bool(market_is_open and live_cycle > 0 and live_cycle <= transition_cycles)

    state["transition_cycles"] = transition_cycles
    state["active"] = active
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    return {
        "market_is_open": bool(market_is_open),
        "opening_transition_active": active,
        "opening_transition_cycle": live_cycle if market_is_open else None,
        "opening_transition_cycles": transition_cycles,
    }


def _get_live_shadow_components(
    md,
    *,
    timeframe_seconds: int,
    top_n: int,
) -> tuple[Layer1StockRanker, Layer2PortfolioBuilder]:
    shadow = app_state.setdefault("layers", {}).setdefault("live_strategy_shadow", {})

    reset_required = (
        shadow.get("timeframe_seconds") != timeframe_seconds
        or shadow.get("top_n") != top_n
        or shadow.get("ranker") is None
        or shadow.get("portfolio_builder") is None
    )

    if reset_required:
        shadow["ranker"] = Layer1StockRanker(md)
        shadow["portfolio_builder"] = Layer2PortfolioBuilder(top_n=top_n)
        shadow["timeframe_seconds"] = timeframe_seconds
        shadow["top_n"] = top_n
        shadow.setdefault("pending_outcomes", [])

        logging.info(
            "[LiveStrategyShadow] Initialized independent live-bar shadow Layer 1/2 | "
            "timeframe_seconds=%s top_n=%s",
            timeframe_seconds,
            top_n,
        )

    return shadow["ranker"], shadow["portfolio_builder"]


def _live_preference_direction(delta: float, deadband: float = 0.0025) -> str:
    if delta > deadband:
        return "LIVE_OVERWEIGHT"
    if delta < -deadband:
        return "LIVE_UNDERWEIGHT"
    return "NEUTRAL"


def _preference_sign(direction: str) -> int:
    if direction == "LIVE_OVERWEIGHT":
        return 1
    if direction == "LIVE_UNDERWEIGHT":
        return -1
    return 0


def _preference_result(score) -> str | None:
    if score is None:
        return None
    score = safe_float(score, 0.0)
    if score > 0:
        return "live_preference_helped"
    if score < 0:
        return "live_preference_hurt"
    return "neutral"


def _append_mature_live_strategy_shadow_outcomes(*, market_is_open: bool) -> None:
    shadow = app_state.setdefault("layers", {}).setdefault("live_strategy_shadow", {})
    pending = shadow.setdefault("pending_outcomes", [])

    if not pending:
        return

    md = app_state.get("market_data", {}).get("buffer")
    now_epoch = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    matured_rows = []
    keep = []

    for item in pending:
        try:
            symbol = str(item.get("symbol") or "").upper().strip()
            created_epoch = safe_float(item.get("created_epoch"), 0.0)
            start_price = safe_float(item.get("start_live_price"), 0.0)

            if not symbol or created_epoch <= 0 or start_price <= 0:
                continue

            current_price = _latest_live_price_for_symbol(md, symbol)
            age_seconds = now_epoch - created_epoch

            if current_price <= 0:
                keep.append(item)
                continue

            for seconds, key in ((600, "10m"), (1800, "30m"), (3600, "60m")):
                field = f"forward_return_{key}"
                if age_seconds >= seconds and field not in item:
                    item[field] = round((current_price - start_price) / start_price, 6)

            has_10m = "forward_return_10m" in item
            has_30m = "forward_return_30m" in item
            has_60m = "forward_return_60m" in item

            finalized_reason = None
            if has_10m and has_30m and has_60m:
                finalized_reason = "all_horizons_complete"
            elif not market_is_open and has_10m:
                finalized_reason = "market_closed_partial"

            if not finalized_reason:
                keep.append(item)
                continue

            direction = item.get("live_preference_direction")
            sign = _preference_sign(direction)

            def pref_score(key: str):
                value = item.get(f"forward_return_{key}")
                if value is None or sign == 0:
                    return None
                return round(sign * safe_float(value, 0.0), 6)

            score_10m = pref_score("10m")
            score_30m = pref_score("30m")
            score_60m = pref_score("60m")

            matured_rows.append({
                "source_timestamp": item.get("source_timestamp"),
                "outcome_timestamp": now_iso,
                "source_cycle_id": item.get("cycle_id"),
                "symbol": symbol,
                "market_is_open_at_source": item.get("market_is_open"),
                "rest_status": item.get("rest_status"),
                "live_status": item.get("live_status"),
                "live_timeframe_seconds": item.get("live_timeframe_seconds"),
                "rest_rank": item.get("rest_rank"),
                "live_rank": item.get("live_rank"),
                "rest_score": item.get("rest_score"),
                "live_score": item.get("live_score"),
                "current_weight": item.get("current_weight"),
                "rest_target_weight": item.get("rest_target_weight"),
                "live_target_weight": item.get("live_target_weight"),
                "target_weight_delta_live_minus_rest": item.get("target_weight_delta_live_minus_rest"),
                "rest_decision": item.get("rest_decision"),
                "live_shadow_decision": item.get("live_shadow_decision"),
                "decision_agreement": item.get("decision_agreement"),
                "live_preference_direction": direction,
                "start_live_price": start_price,
                "outcome_live_price": current_price,
                "forward_return_10m": item.get("forward_return_10m"),
                "forward_return_30m": item.get("forward_return_30m"),
                "forward_return_60m": item.get("forward_return_60m"),
                "live_preference_score_10m": score_10m,
                "live_preference_score_30m": score_30m,
                "live_preference_score_60m": score_60m,
                "live_preference_result_10m": _preference_result(score_10m),
                "live_preference_result_30m": _preference_result(score_30m),
                "live_preference_result_60m": _preference_result(score_60m),
                "finalized_reason": finalized_reason,
            })

        except Exception:
            logging.warning(
                "[LiveStrategyShadow] Failed finalizing one pending outcome row.",
                exc_info=True,
            )
            keep.append(item)

    shadow["pending_outcomes"] = keep

    if matured_rows:
        append_layer_live_strategy_outcome_rows(matured_rows)
        logging.info(
            "[LiveStrategyShadow] Appended matured outcome rows | count=%s pending=%s",
            len(matured_rows),
            len(keep),
        )


def run_live_strategy_shadow_comparison(
    *,
    symbols: list[str],
    cycle_id: int | None,
    market_is_open: bool,
    rest_ranked: list | None,
    rest_target: dict | None,
    rest_status: str,
    layer3_plan: list[dict] | None = None,
    layer3_summary: dict | None = None,
    rest_bars_by_symbol: dict | None = None,
    allow_closed_market: bool = False,
    record_outcomes: bool = True,
) -> dict:
    """
    Run an independent live-bar Layer 1/2 shadow evaluation and log comparison CSVs.

    This is intentionally side-effect isolated from the real Layer 1/2/3 path:
    - does not update app_state["layers"]["latest"]
    - does not call run_layer3_dry_run()
    - does not submit orders
    """
    enabled = bool(_execution_setting("live_strategy_shadow_enabled", True))
    if not enabled:
        _append_mature_live_strategy_shadow_outcomes(market_is_open=market_is_open)
        return {"enabled": enabled, "status": "disabled"}

    if not market_is_open and not allow_closed_market:
        _append_mature_live_strategy_shadow_outcomes(market_is_open=market_is_open)
        return {"enabled": enabled, "status": "market_closed"}

    md = app_state.get("market_data", {}).get("buffer")
    if md is None or not hasattr(md, "get_live_bars"):
        return {"enabled": enabled, "status": "missing_market_data_buffer"}

    _append_mature_live_strategy_shadow_outcomes(market_is_open=market_is_open)

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    now_epoch = time.time()

    timeframe_seconds = safe_int(
        _execution_setting("live_strategy_shadow_timeframe_seconds", 60),
        60,
    )
    live_bar_limit = safe_int(
        _execution_setting("live_strategy_shadow_bar_limit", 500),
        500,
    )
    min_live_bars = safe_int(
        _execution_setting("live_strategy_shadow_min_bars", 61),
        61,
    )
    top_n = safe_int(
        _execution_setting("live_strategy_shadow_top_n", 5),
        5,
    )
    drift_threshold = safe_float(
        _execution_setting("live_strategy_shadow_min_abs_weight_drift", 0.025),
        0.025,
    )

    live_bars_by_symbol, live_bar_counts, live_prices = _build_live_bars_by_symbol(
        symbols,
        timeframe_seconds=timeframe_seconds,
        limit=live_bar_limit,
    )

    live_symbols_ready = [
        symbol
        for symbol, count in live_bar_counts.items()
        if count >= min_live_bars
    ]

    cycle_base = {
        "timestamp": now_iso,
        "cycle_id": cycle_id,
        "market_is_open": market_is_open,
        "rest_status": rest_status,
        "live_timeframe_seconds": timeframe_seconds,
        "live_min_required_bars": min_live_bars,
        "live_symbols_ready": live_symbols_ready,
        "symbol_count": len(symbols or []),
        "rest_ranked_count": len(rest_ranked or []),
    }

    if not live_symbols_ready:
        row = {
            **cycle_base,
            "live_status": "insufficient_live_bars",
            "live_ranked_count": 0,
            "error": None,
        }
        append_layer_live_strategy_shadow_cycle_row(row)
        logging.info(
            "[LiveStrategyShadow] Insufficient live bars | ready=%s/%s min_bars=%s timeframe=%ss counts=%s",
            len(live_symbols_ready),
            len(symbols or []),
            min_live_bars,
            timeframe_seconds,
            live_bar_counts,
        )
        return row

    try:
        ranker, portfolio_builder = _get_live_shadow_components(
            md,
            timeframe_seconds=timeframe_seconds,
            top_n=top_n,
        )

        live_ranked = ranker.rank_from_bars(live_bars_by_symbol)
        live_target = portfolio_builder.build_target_portfolio(
            live_ranked,
            context=_live_shadow_evaluation_context(
                market_is_open=market_is_open,
                count_live_cycle=market_is_open,
            ),
        )

        live_status = "ok" if live_ranked else "no_live_ranked_symbols"

    except Exception as exc:
        logging.warning("[LiveStrategyShadow] Evaluation failed.", exc_info=True)
        row = {
            **cycle_base,
            "live_status": "error",
            "live_ranked_count": 0,
            "error": str(exc),
        }
        append_layer_live_strategy_shadow_cycle_row(row)
        return row

    rest_rank_map, rest_top_symbols = _rank_snapshot(rest_ranked)
    live_rank_map, live_top_symbols = _rank_snapshot(live_ranked)

    rest_target = rest_target or {}
    live_target = live_target or {}
    rest_meta = _target_meta(rest_target)
    live_meta = _target_meta(live_target)

    layer3_plan = layer3_plan or []
    layer3_summary = layer3_summary or {}
    rest_bars_by_symbol = rest_bars_by_symbol or {}

    plan_by_symbol = {
        str(row.get("symbol") or "").upper().strip(): row
        for row in layer3_plan
        if isinstance(row, dict) and row.get("symbol")
    }

    equity = safe_float(layer3_summary.get("equity"), 0.0)

    symbols_for_rows = sorted(
        set(str(s or "").upper().strip() for s in symbols or [] if str(s or "").strip())
        | _target_symbols(rest_target)
        | _target_symbols(live_target)
        | set(plan_by_symbol.keys())
    )

    rows = []
    pending_rows = []
    agreement_values = []
    total_abs_target_diff = 0.0
    max_abs_target_diff = 0.0
    rest_decision_counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    live_decision_counts = {"BUY": 0, "SELL": 0, "HOLD": 0, "NOT_ESTIMATED": 0}

    for symbol in symbols_for_rows:
        plan_row = plan_by_symbol.get(symbol, {})
        rest_info = rest_rank_map.get(symbol, {})
        live_info = live_rank_map.get(symbol, {})

        rest_rank = rest_info.get("rank")
        live_rank = live_info.get("rank")

        rest_score = rest_info.get("score")
        live_score = live_info.get("score")

        rest_weight = _target_weight(rest_target, symbol)
        live_weight = _target_weight(live_target, symbol)
        target_delta = live_weight - rest_weight
        abs_target_delta = abs(target_delta)
        total_abs_target_diff += abs_target_delta
        max_abs_target_diff = max(max_abs_target_diff, abs_target_delta)

        current_weight = (
            safe_float(plan_row.get("current_weight"), 0.0)
            if plan_row else None
        )

        rest_decision = str(plan_row.get("decision") or "HOLD").upper().strip() if plan_row else "NOT_ESTIMATED"
        if rest_decision in rest_decision_counts:
            rest_decision_counts[rest_decision] += 1

        if current_weight is None:
            live_decision = "NOT_ESTIMATED"
        else:
            live_delta = live_weight - current_weight
            if abs(live_delta) < drift_threshold:
                live_decision = "HOLD"
            elif live_delta > 0:
                live_decision = "BUY"
            else:
                live_decision = "SELL"

        live_decision_counts[live_decision] = live_decision_counts.get(live_decision, 0) + 1

        decision_agreement = (
            rest_decision == live_decision
            if rest_decision != "NOT_ESTIMATED" and live_decision != "NOT_ESTIMATED"
            else None
        )
        if decision_agreement is not None:
            agreement_values.append(bool(decision_agreement))

        rest_price = safe_float(rest_info.get("last_price"), 0.0)
        if rest_price <= 0 and rest_bars_by_symbol.get(symbol):
            rest_price = safe_float(rest_bars_by_symbol[symbol][-1].get("close"), 0.0)

        live_price = safe_float(live_prices.get(symbol), 0.0)
        live_vs_rest_price_pct = (
            round((live_price - rest_price) / rest_price, 6)
            if live_price > 0 and rest_price > 0
            else None
        )

        rank_delta = (
            live_rank - rest_rank
            if isinstance(live_rank, int) and isinstance(rest_rank, int)
            else None
        )

        score_delta = (
            round(safe_float(live_score, 0.0) - safe_float(rest_score, 0.0), 6)
            if live_score is not None and rest_score is not None
            else None
        )

        preference_direction = _live_preference_direction(target_delta)
        live_estimated_notional = (
            round(abs((live_weight - current_weight) * equity), 2)
            if current_weight is not None and equity > 0
            else None
        )

        row = {
            "timestamp": now_iso,
            "cycle_id": cycle_id,
            "symbol": symbol,
            "market_is_open": market_is_open,
            "rest_status": rest_status,
            "live_status": live_status,
            "live_timeframe_seconds": timeframe_seconds,
            "live_bar_count": live_bar_counts.get(symbol),
            "rest_bar_count": len(rest_bars_by_symbol.get(symbol, []) or []),
            "rest_rank": rest_rank,
            "live_rank": live_rank,
            "rank_delta_live_minus_rest": rank_delta,
            "rest_score": rest_score,
            "live_score": live_score,
            "score_delta_live_minus_rest": score_delta,
            "current_weight": current_weight,
            "rest_target_weight": rest_weight,
            "live_target_weight": live_weight,
            "target_weight_delta_live_minus_rest": round(target_delta, 6),
            "rest_decision": rest_decision,
            "live_shadow_decision": live_decision,
            "decision_agreement": decision_agreement,
            "live_preference_direction": preference_direction,
            "rest_planned_qty": plan_row.get("planned_qty") if plan_row else None,
            "rest_planned_notional": plan_row.get("planned_notional") if plan_row else None,
            "live_shadow_estimated_notional": live_estimated_notional,
            "rest_price": rest_price if rest_price > 0 else None,
            "live_price": live_price if live_price > 0 else None,
            "live_vs_rest_price_pct": live_vs_rest_price_pct,
            "rest_reason": plan_row.get("reason") if plan_row else None,
            "live_reason": live_info.get("reason"),
            "rest_top_symbols": rest_top_symbols,
            "live_top_symbols": live_top_symbols,
            "rest_target_summary": target_summary_for_log(rest_target),
            "live_target_summary": target_summary_for_log(live_target),
        }
        rows.append(row)

        if record_outcomes and market_is_open and live_price > 0:
            pending_rows.append({
                "created_epoch": now_epoch,
                "source_timestamp": now_iso,
                "cycle_id": cycle_id,
                "symbol": symbol,
                "market_is_open": market_is_open,
                "rest_status": rest_status,
                "live_status": live_status,
                "live_timeframe_seconds": timeframe_seconds,
                "rest_rank": rest_rank,
                "live_rank": live_rank,
                "rest_score": rest_score,
                "live_score": live_score,
                "current_weight": current_weight,
                "rest_target_weight": rest_weight,
                "live_target_weight": live_weight,
                "target_weight_delta_live_minus_rest": round(target_delta, 6),
                "rest_decision": rest_decision,
                "live_shadow_decision": live_decision,
                "decision_agreement": decision_agreement,
                "live_preference_direction": preference_direction,
                "start_live_price": live_price,
            })

    append_layer_live_strategy_shadow_rows(rows)

    shadow_state = app_state.setdefault("layers", {}).setdefault("live_strategy_shadow", {})
    pending = shadow_state.setdefault("pending_outcomes", [])
    pending.extend(pending_rows)
    del pending[:-5000]

    rest_top_set = set(rest_top_symbols)
    live_top_set = set(live_top_symbols)
    overlap_symbols = sorted(rest_top_set & live_top_set)
    row_count = max(len(symbols_for_rows), 1)
    decision_agreement_rate = (
        round(sum(1 for v in agreement_values if v) / len(agreement_values), 4)
        if agreement_values else None
    )

    cycle_row = {
        **cycle_base,
        "live_status": live_status,
        "live_ranked_count": len(live_ranked or []),
        "rest_top_symbols": rest_top_symbols,
        "live_top_symbols": live_top_symbols,
        "top5_overlap_count": len(overlap_symbols),
        "top5_overlap_symbols": overlap_symbols,
        "total_abs_target_weight_diff": round(total_abs_target_diff, 6),
        "avg_abs_target_weight_diff": round(total_abs_target_diff / row_count, 6),
        "max_abs_target_weight_diff": round(max_abs_target_diff, 6),
        "decision_agreement_rate": decision_agreement_rate,
        "rest_buy_count": rest_decision_counts.get("BUY", 0),
        "rest_sell_count": rest_decision_counts.get("SELL", 0),
        "rest_hold_count": rest_decision_counts.get("HOLD", 0),
        "live_buy_count": live_decision_counts.get("BUY", 0),
        "live_sell_count": live_decision_counts.get("SELL", 0),
        "live_hold_count": live_decision_counts.get("HOLD", 0),
        "live_not_estimated_count": live_decision_counts.get("NOT_ESTIMATED", 0),
        "live_cash_pct": live_target.get("CASH") if isinstance(live_target, dict) else None,
        "rest_cash_pct": rest_target.get("CASH") if isinstance(rest_target, dict) else None,
        "live_market_strength": live_meta.get("market_strength"),
        "rest_market_strength": rest_meta.get("market_strength"),
        "error": None,
    }

    append_layer_live_strategy_shadow_cycle_row(cycle_row)

    logging.info(
        "[LiveStrategyShadow] Complete | cycle_id=%s live_status=%s live_ranked=%s "
        "rest_ranked=%s top5_overlap=%s target_diff_total=%.4f decision_agreement=%s pending_outcomes=%s",
        cycle_id,
        live_status,
        len(live_ranked or []),
        len(rest_ranked or []),
        len(overlap_symbols),
        total_abs_target_diff,
        decision_agreement_rate,
        len(pending),
    )

    return cycle_row


async def run_layer_monitor(interval_seconds: int = 600) -> None:
    """
    Runs Layer 1/2 evaluation on a timer.

    Layer 1/2 builds the target portfolio.
    Layer 3 builds a rebalance plan.

    Order execution is controlled by LAYER3_EXECUTION_ENABLED.
    When disabled, Layer 3 remains dry-run only.
    """
    logging.info(
        "[Layers] Layer monitor started | interval_seconds=%s wall_clock_aligned=True",
        normalize_layer_interval_seconds(interval_seconds),
    )

    while not app_state["stream"]["shutdown_event"].is_set():
        try:
            layer_engine = app_state.get("layers", {}).get("engine")

            if not layer_engine:
                logging.info("[Layers] Engine not initialized yet. Skipping.")
            else:
                symbols = app_state.get("main", {}).get("symbol", [])

                if not symbols:
                    logging.info("[Layers] No symbols configured. Skipping.")
                else:
                    logging.info("[Layers] Starting scheduled evaluation.")

                    market_is_open = get_market_is_open(app_state)

                    run_24_7 = bool(
                        _execution_setting("layer_monitor_run_24_7", True)
                    )

                    if not run_24_7 and not market_is_open:
                        logging.info(
                            "[Layers] Market closed and layer_monitor_run_24_7=false. "
                            "Skipping this cycle."
                        )

                        append_layer_cycle_row(
                            status="skipped",
                            reason="market_closed_run_24_7_false",
                            market_is_open=market_is_open,
                            ranked_count=0,
                        )

                        continue

                    logging.info(
                        "[Layers] Market status | market_is_open=%s run_24_7=%s",
                        market_is_open,
                        run_24_7,
                    )

                    md = app_state.get("market_data", {}).get("buffer")
                    if md:
                        tick_counts = {
                            symbol: len(md.get_recent_prices(symbol))
                            for symbol in symbols
                        }
                        logging.info("[Layers] Tick counts: %s", tick_counts)

                    required_fresh_symbols = _fresh_symbol_requirement(len(symbols))

                    bars_by_symbol = fetch_recent_bars_with_min_count(
                        app_state.get("stock_data_client"),
                        symbols,
                        min_bars=180,
                        timeframe_minutes=5,
                        initial_lookback_hours=96,
                        max_lookback_hours=336,
                        min_ready_symbols=required_fresh_symbols,
                    )

                    if market_is_open:
                        logging.info("[Layers] Symbols being evaluated: %s", symbols)
                    else:
                        logging.info("[Layers] Symbols being observed: %s", symbols)

                    bar_counts = {
                        symbol: len(bars_by_symbol.get(symbol, []))
                        for symbol in symbols
                    }
                    logging.info("[Layers] Bar counts: %s", bar_counts)

                    symbols_with_60_bars = [
                        symbol
                        for symbol, count in bar_counts.items()
                        if count >= 60
                    ]

                    symbols_with_180_bars = [
                        symbol
                        for symbol, count in bar_counts.items()
                        if count >= 180
                    ]

                    logging.info(
                        "[Layers] Bar readiness | >=60 bars=%s/%s >=180 bars=%s/%s",
                        len(symbols_with_60_bars),
                        len(symbols),
                        len(symbols_with_180_bars),
                        len(symbols),
                    )

                    freshness_market_hours_only = bool(
                        _execution_setting("bar_freshness_market_hours_only", True)
                    )

                    freshness_required = (
                        market_is_open or not freshness_market_hours_only
                    )

                    max_age_minutes = safe_float(
                        _execution_setting("bar_freshness_max_age_minutes", 35.0),
                        35.0,
                    )

                    freshness_probe_bars_by_symbol, freshness_report = filter_fresh_bars(
                        bars_by_symbol,
                        symbols,
                        max_age_minutes=max_age_minutes,
                    )

                    if freshness_required:
                        fresh_bars_by_symbol = freshness_probe_bars_by_symbol
                    else:
                        # Off-hours freshness is diagnostic only. We still evaluate
                        # the full bar set for warmup smoothing, but keep the real
                        # age/fresh/stale report so Layer 3 bootstrap can avoid
                        # trusting stale warmup symbols at the open.
                        fresh_bars_by_symbol = bars_by_symbol

                    freshness_report["freshness_required"] = freshness_required
                    freshness_report["market_is_open"] = market_is_open
                    freshness_report["required_fresh_symbols"] = required_fresh_symbols

                    app_state.setdefault("layers", {})["bar_freshness"] = freshness_report

                    next_layer3_cycle_id = int(
                        app_state.get("layers", {})
                        .get("rebalance", {})
                        .get("last_cycle_id", 0)
                        or 0
                    ) + 1

                    _append_live_bar_health_snapshot(
                        symbols=symbols,
                        rest_bars_by_symbol=bars_by_symbol,
                        market_is_open=market_is_open,
                        cycle_id=next_layer3_cycle_id if market_is_open else None,
                    )

                    if not freshness_required:
                        logging.info(
                            "[Bars] Freshness check | required=False market_is_open=%s "
                            "status=not_enforced fresh=%s/%s stale=%s missing=%s ages=%s "
                            "max_age_minutes=%s",
                            market_is_open,
                            freshness_report.get("fresh_count", 0),
                            freshness_report.get("total_symbols", len(symbols or [])),
                            freshness_report.get("stale_symbols", []),
                            freshness_report.get("missing_symbols", []),
                            freshness_report.get("latest_bar_ages_minutes", {}),
                            max_age_minutes,
                        )

                    else:

                        logging.info(
                            "[Bars] Freshness check | required=True market_is_open=%s "
                            "max_age_minutes=%s fresh=%s/%s required_fresh_symbols=%s "
                            "stale=%s missing=%s ages=%s",
                            market_is_open,
                            max_age_minutes,
                            freshness_report.get("fresh_count", 0),
                            freshness_report.get("total_symbols", len(symbols or [])),
                            required_fresh_symbols,
                            freshness_report.get("stale_symbols", []),
                            freshness_report.get("missing_symbols", []),
                            freshness_report.get("latest_bar_ages_minutes", {}),
                        )

                        if freshness_required and freshness_report["fresh_count"] < required_fresh_symbols:
                            skip_info = {
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "reason": "insufficient_fresh_bars",
                                "market_is_open": market_is_open,
                                "fresh_count": freshness_report["fresh_count"],
                                "required_fresh_symbols": required_fresh_symbols,
                                "stale_symbols": freshness_report["stale_symbols"],
                                "missing_symbols": freshness_report["missing_symbols"],
                                "latest_bar_ages_minutes": freshness_report["latest_bar_ages_minutes"],
                            }

                            app_state.setdefault("layers", {})["last_skipped_evaluation"] = skip_info

                            logging.warning(
                                "[Layers] Skipping Layer 1/2/3/4 because insufficient fresh bars. "
                                "skip_info=%s",
                                skip_info,
                            )

                            # Even when the REST-bar path skips, run the live-bar shadow comparison
                            # so we can measure whether live bars had enough information to produce
                            # a usable target earlier than delayed REST bars.
                            run_live_strategy_shadow_comparison(
                                symbols=symbols,
                                cycle_id=next_layer3_cycle_id,
                                market_is_open=market_is_open,
                                rest_ranked=[],
                                rest_target={},
                                rest_status="skipped_insufficient_fresh_bars",
                                layer3_plan=[],
                                layer3_summary={},
                                rest_bars_by_symbol=bars_by_symbol,
                            )

                            append_layer_cycle_row(
                                status="skipped",
                                reason="insufficient_fresh_bars",
                                market_is_open=market_is_open,
                                fresh_count=freshness_report.get("fresh_count"),
                                required_fresh_symbols=required_fresh_symbols,
                                ranked_count=0,
                            )

                            continue

                    evaluation_symbols = list(fresh_bars_by_symbol.keys())

                    if not evaluation_symbols:
                        logging.warning(
                            "[Layers] Skipping Layer 1/2/3/4 because no evaluation symbols are available."
                        )

                        append_layer_cycle_row(
                            status="skipped",
                            reason="no_evaluation_symbols",
                            market_is_open=market_is_open,
                            fresh_count=freshness_report.get("fresh_count") if isinstance(freshness_report, dict) else None,
                            required_fresh_symbols=required_fresh_symbols,
                            ranked_count=0,
                        )

                        continue

                    if not market_is_open:
                        result = layer_engine.evaluate(
                            evaluation_symbols,
                            bars_by_symbol=fresh_bars_by_symbol,
                            context=_layer2_evaluation_context(
                                market_is_open=False,
                                count_live_cycle=False,
                            ),
                        )

                        ranked = result.get("ranked", [])
                        target = result.get("target_portfolio", {})

                        fresh_bar_counts = {
                            symbol: len(fresh_bars_by_symbol.get(symbol, []))
                            for symbol in evaluation_symbols
                        }

                        store_off_hours_layer_warmup_result(
                            symbols=evaluation_symbols,
                            bar_counts=fresh_bar_counts,
                            ranked=ranked,
                            target=target,
                            freshness_report=freshness_report,
                        )

                        run_live_strategy_shadow_comparison(
                            symbols=evaluation_symbols,
                            cycle_id=None,
                            market_is_open=False,
                            rest_ranked=ranked,
                            rest_target=target,
                            rest_status="market_closed_warmup",
                            layer3_plan=[],
                            layer3_summary={},
                            rest_bars_by_symbol=fresh_bars_by_symbol,
                            allow_closed_market=True,
                            record_outcomes=False,
                        )

                        append_layer_cycle_row(
                            status="warmup_only",
                            reason="market_closed_target_warmup",
                            market_is_open=market_is_open,
                            fresh_count=freshness_report.get("fresh_count") if isinstance(freshness_report, dict) else None,
                            required_fresh_symbols=required_fresh_symbols,
                            ranked_count=len(ranked or []),
                            top_symbols=[
                                getattr(r, "symbol", None)
                                for r in (ranked or [])[:5]
                            ],
                            target_summary=target_summary_for_log(target),
                            layer3_summary={},
                            layer4_result={},
                        )

                        logging.info(
                            "[Layers] Market closed; completed Layer 1/2 target warmup. "
                            "Skipping Layer 3 planning and Layer 4 execution."
                        )

                        _expire_active_plan_for_market_close()
                        _append_mature_live_strategy_shadow_outcomes(market_is_open=False)

                        continue

                    result = layer_engine.evaluate(
                        evaluation_symbols,
                        bars_by_symbol=fresh_bars_by_symbol,
                        context=_layer2_evaluation_context(
                            market_is_open=True,
                            count_live_cycle=True,
                        ),
                    )

                    logging.info("[Layers] Evaluation complete.")

                    ranked = result.get("ranked", [])
                    target = result.get("target_portfolio", {})

                    logging.info(
                        "[Layers] Ranked count=%s target_summary=%s",
                        len(ranked),
                        target_summary_for_log(target),
                    )

                    fresh_bar_counts = {
                        symbol: len(fresh_bars_by_symbol.get(symbol, []))
                        for symbol in evaluation_symbols
                    }

                    next_layer3_cycle_id = int(
                        app_state.get("layers", {})
                        .get("rebalance", {})
                        .get("last_cycle_id", 0)
                        or 0
                    ) + 1

                    store_latest_layer_result(
                        symbols=evaluation_symbols,
                        bar_counts=fresh_bar_counts,
                        ranked=ranked,
                        target=target,
                    )

                    layer3_result = run_layer3_dry_run()

                    if isinstance(layer3_result, dict) and (
                        "plan" in layer3_result or "summary" in layer3_result
                    ):
                        layer3_plan = layer3_result.get("plan", [])
                        layer3_summary = layer3_result.get("summary", {})
                    else:
                        # Backward-compatible fallback:
                        # If run_layer3_dry_run() still returns only a summary,
                        # read the plan/summary from app_state["layers"]["rebalance"].
                        rebalance = app_state.get("layers", {}).get("rebalance", {})
                        layer3_plan = rebalance.get("last_plan", [])
                        layer3_summary = (
                            layer3_result
                            if isinstance(layer3_result, dict)
                            else rebalance.get("last_summary", {})
                        )

                    logging.info(
                        "[Layer3] Plan summary | cycle_id=%s plan_id=%s status=%s decisions=%s "
                        "equity=$%s cash=$%s target_symbols=%s target_cash_pct=%s "
                        "open_orders=%s fail_safe_active=%s opening_transition=%s open_cycle=%s "
                        "warmup_stale=%s warmup_skipped=%s",
                        layer3_summary.get("cycle_id"),
                        layer3_summary.get("plan_id"),
                        layer3_summary.get("status"),
                        layer3_summary.get("decision_counts"),
                        layer3_summary.get("equity"),
                        layer3_summary.get("cash"),
                        layer3_summary.get("target_symbol_count"),
                        layer3_summary.get("target_cash_pct"),
                        layer3_summary.get("open_order_count"),
                        layer3_summary.get("fail_safe_active"),
                        layer3_summary.get("opening_transition_active"),
                        layer3_summary.get("open_session_live_cycle_count"),
                        layer3_summary.get("bootstrap_confirmation_warmup_stale_symbols"),
                        layer3_summary.get("bootstrap_confirmation_warmup_skipped_symbols"),
                    )

                    run_live_strategy_shadow_comparison(
                        symbols=evaluation_symbols,
                        cycle_id=next_layer3_cycle_id,
                        market_is_open=market_is_open,
                        rest_ranked=ranked,
                        rest_target=target,
                        rest_status="ok",
                        layer3_plan=layer3_plan,
                        layer3_summary=layer3_summary,
                        rest_bars_by_symbol=fresh_bars_by_symbol,
                    )

                    layer4_execution_result = execute_layer4_plan(
                        layer3_plan,
                        layer3_summary,
                    )

                    append_layer_cycle_row(
                        status="ok",
                        reason=None,
                        market_is_open=market_is_open,
                        fresh_count=freshness_report.get("fresh_count") if isinstance(freshness_report, dict) else None,
                        required_fresh_symbols=required_fresh_symbols,
                        ranked_count=len(ranked or []),
                        top_symbols=[
                            getattr(r, "symbol", None)
                            for r in (ranked or [])[:5]
                        ],
                        target_summary=target_summary_for_log(target),
                        layer3_summary=layer3_summary,
                        layer4_result=layer4_execution_result,
                    )

                    logging.info(
                        "[Layers] Layer4 handoff complete | cycle_id=%s plan_id=%s attempted=%s "
                        "submitted=%s skipped=%s errors=%s blocked_reason=%s count_integrity_ok=%s order_count=%s",
                        layer4_execution_result.get("cycle_id"),
                        layer4_execution_result.get("plan_id"),
                        layer4_execution_result.get("attempted"),
                        layer4_execution_result.get("submitted"),
                        layer4_execution_result.get("skipped"),
                        layer4_execution_result.get("errors"),
                        layer4_execution_result.get("blocked_reason"),
                        layer4_execution_result.get("count_integrity_ok"),
                        len(layer4_execution_result.get("orders", []) or []),
                    )

                    if not ranked:
                        logging.info(
                            "[Layers] No ranked symbols yet. Likely not enough market data. "
                            "This is normal during startup/off-hours."
                        )
                    else:
                        top_symbols = [
                            f"{r.symbol}:{r.score:.4f}"
                            for r in ranked[:5]
                        ]

                        logging.info(
                            "[Layers] Evaluation @ %s | Top Ranked: %s",
                            datetime.now(timezone.utc).isoformat(),
                            top_symbols,
                        )

                        logging.info(
                            "[Layers] Target summary: %s",
                            target_summary_for_log(target),
                        )

        except asyncio.CancelledError:
            logging.info("[Layers] Layer monitor cancelled.")

            append_layer_cycle_row(
                status="cancelled",
                reason="layer_monitor_exception",
            )

            raise

        except Exception:
            logging.exception("[Layers] Layer monitor evaluation failed.")

            append_layer_cycle_row(
                status="error",
                reason="layer_monitor_exception",
            )

        finally:
            if not app_state["stream"]["shutdown_event"].is_set():
                await sleep_until_next_layer_boundary(
                    shutdown_event=app_state["stream"]["shutdown_event"],
                    interval_seconds=interval_seconds,
                    min_spacing_seconds=180.0,
                )

    logging.info("[Layers] Layer monitor exited cleanly.")