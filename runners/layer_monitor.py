# runners/layer_monitor.py

import asyncio
import logging
import math
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
from layers.layer3_rebalancer import run_layer3_dry_run
from layers.layer4_executor import execute_layer4_plan
from layers.layer_csv import append_layer_cycle_row


def _execution_setting(name: str, default):
    return app_state.get("execution", {}).get(
        name,
        getattr(config, name.upper(), default),
    )


def _live_bar_health_snapshot(symbols: list[str]) -> dict:
    md = app_state.get("market_data", {}).get("buffer")
    if not md:
        return {}

    out = {}

    for symbol in symbols or []:
        try:
            prices = list(md.get_recent_prices(symbol) or [])
            bars_1m = (
                list(md.get_live_bars(symbol, timeframe_seconds=60, limit=3) or [])
                if hasattr(md, "get_live_bars")
                else []
            )
            bars_5m = (
                list(md.get_live_bars(symbol, timeframe_seconds=300, limit=3) or [])
                if hasattr(md, "get_live_bars")
                else []
            )

            latest_1m = bars_1m[-1] if bars_1m else {}
            latest_5m = bars_5m[-1] if bars_5m else {}

            out[symbol] = {
                "tick_count": len(prices),
                "live_price": prices[-1] if prices else None,
                "live_1m_bar_count": len(bars_1m),
                "live_5m_bar_count": len(bars_5m),
                "latest_1m_close": latest_1m.get("close"),
                "latest_5m_close": latest_5m.get("close"),
                "latest_1m_volume": latest_1m.get("volume"),
                "latest_5m_volume": latest_5m.get("volume"),
                "latest_1m_trade_count": latest_1m.get("trade_count"),
                "latest_5m_trade_count": latest_5m.get("trade_count"),
            }

        except Exception as exc:
            out[symbol] = {
                "error": str(exc),
            }

    return out


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

                        live_bar_health = _live_bar_health_snapshot(symbols)
                        app_state.setdefault("layers", {})["live_bar_health"] = live_bar_health

                        logging.info(
                            "[LiveBars] Health snapshot | %s",
                            live_bar_health,
                        )

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