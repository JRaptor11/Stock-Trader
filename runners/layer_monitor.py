import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta

from config import runtime_config as config
from market.bar_data import (
    fetch_recent_bars_with_min_count,
    filter_fresh_bars,
)

from core.state import app_state
from layers.layer3_rebalancer import run_layer3_dry_run
from layers.layer4_executor import execute_layer4_plan


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _execution_setting(name: str, default):
    return app_state.get("execution", {}).get(
        name,
        getattr(config, name.upper(), default),
    )

def _normalize_layer_interval_seconds(interval_seconds: int) -> int:
    try:
        interval_seconds = int(interval_seconds)
    except Exception:
        interval_seconds = 600

    if interval_seconds <= 0:
        return 600

    # For wall-clock alignment, the interval should divide evenly into 1 hour.
    # 600 = every 10 minutes: :00, :10, :20, :30, :40, :50.
    if 3600 % interval_seconds != 0:
        logging.warning(
            "[Layers] interval_seconds=%s does not divide evenly into 1 hour. "
            "Defaulting to 600 seconds for wall-clock alignment.",
            interval_seconds,
        )
        return 600

    return interval_seconds


def _next_wall_clock_run_time(
    interval_seconds: int,
    now: datetime | None = None,
) -> datetime:
    interval_seconds = _normalize_layer_interval_seconds(interval_seconds)

    now = now or datetime.now(timezone.utc)

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    hour_start = now.replace(minute=0, second=0, microsecond=0)

    seconds_since_hour = (
        now.minute * 60
        + now.second
        + now.microsecond / 1_000_000
    )

    next_slot_seconds = (
        math.floor(seconds_since_hour / interval_seconds) + 1
    ) * interval_seconds

    if next_slot_seconds >= 3600:
        return hour_start + timedelta(hours=1)

    return hour_start + timedelta(seconds=next_slot_seconds)


async def _sleep_until_next_layer_boundary(interval_seconds: int) -> None:
    shutdown_event = app_state["stream"]["shutdown_event"]

    interval_seconds = _normalize_layer_interval_seconds(interval_seconds)
    min_spacing_seconds = 180.0

    next_run_at = _next_wall_clock_run_time(interval_seconds)
    now = datetime.now(timezone.utc)
    sleep_seconds = max(0.0, (next_run_at - now).total_seconds())

    if sleep_seconds <= min_spacing_seconds:
        skipped_run_at = next_run_at
        next_run_at = next_run_at + timedelta(seconds=interval_seconds)
        sleep_seconds = max(0.0, (next_run_at - now).total_seconds())

        logging.info(
            "[Layers] Next wall-clock cycle is too close; skipping one boundary | "
            "skipped_run_at=%s next_run_at=%s sleep_seconds=%.1f "
            "min_spacing_seconds=%.1f interval_seconds=%s",
            skipped_run_at.isoformat(),
            next_run_at.isoformat(),
            sleep_seconds,
            min_spacing_seconds,
            interval_seconds,
        )

    logging.info(
        "[Layers] Sleeping until next wall-clock cycle | next_run_at=%s "
        "sleep_seconds=%.1f interval_seconds=%s",
        next_run_at.isoformat(),
        sleep_seconds,
        interval_seconds,
    )

    try:
        await asyncio.wait_for(
            asyncio.to_thread(shutdown_event.wait, sleep_seconds),
            timeout=sleep_seconds + 5,
        )
    except asyncio.TimeoutError:
        pass


def _target_summary_for_log(target: dict) -> dict:
    if not isinstance(target, dict):
        return {}

    meta = target.get("_meta", {}) if isinstance(target.get("_meta"), dict) else {}

    weights = {}
    for key, value in target.items():
        if key == "_meta":
            continue
        try:
            weights[key] = round(float(value), 4)
        except Exception:
            weights[key] = value

    return {
        "weights": weights,
        "market_strength": meta.get("market_strength"),
        "cash_pct": meta.get("cash_pct", weights.get("CASH")),
        "investable_pct": meta.get("investable_pct"),
        "top_score": meta.get("top_score"),
        "avg_top_score": meta.get("avg_top_score"),
        "smoothing_applied": meta.get("smoothing_applied"),
        "smoothing_mode": meta.get("smoothing_mode"),
    }


def _get_market_is_open() -> bool:
    """
    Prefer the broker clock. Fall back to market_monitor state if needed.
    """
    client = app_state.get("trading_client")

    if client is not None:
        try:
            clock = client.get_clock()
            return bool(getattr(clock, "is_open", False))
        except Exception:
            logging.warning("[Layers] Could not fetch market clock.", exc_info=True)

    try:
        return bool(
            app_state
            .get("services", {})
            .get("market_monitor", {})
            .get("market_open", False)
        )
    except Exception:
        return False


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
    min_symbols = _safe_int(
        _execution_setting("bar_freshness_min_fresh_symbols", 5),
        5,
    )

    min_ratio = _safe_float(
        _execution_setting("bar_freshness_min_fresh_ratio", 0.70),
        0.70,
    )

    ratio_required = math.ceil(symbol_count * min_ratio)

    return max(1, min(symbol_count, max(min_symbols, ratio_required)))


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
        _target_summary_for_log(target),
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
        _normalize_layer_interval_seconds(interval_seconds),
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

                    market_is_open = _get_market_is_open()

                    run_24_7 = bool(
                        _execution_setting("layer_monitor_run_24_7", True)
                    )

                    if not run_24_7 and not market_is_open:
                        logging.info(
                            "[Layers] Market closed and layer_monitor_run_24_7=false. "
                            "Skipping this cycle."
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

                    max_age_minutes = _safe_float(
                        _execution_setting("bar_freshness_max_age_minutes", 35.0),
                        35.0,
                    )

                    if freshness_required:
                        fresh_bars_by_symbol, freshness_report = filter_fresh_bars(
                            bars_by_symbol,
                            symbols,
                            max_age_minutes=max_age_minutes,
                        )
                    else:
                        fresh_bars_by_symbol = bars_by_symbol
                        freshness_report = {
                            "max_age_minutes": max_age_minutes,
                            "fresh_symbols": list(symbols),
                            "stale_symbols": [],
                            "missing_symbols": [],
                            "fresh_count": len(symbols),
                            "stale_count": 0,
                            "total_symbols": len(symbols),
                            "latest_bar_times": {},
                            "latest_bar_ages_minutes": {},
                            "freshness_required": False,
                        }

                    freshness_report["freshness_required"] = freshness_required
                    freshness_report["market_is_open"] = market_is_open
                    freshness_report["required_fresh_symbols"] = required_fresh_symbols

                    app_state.setdefault("layers", {})["bar_freshness"] = freshness_report

                    if not freshness_required:
                        logging.info(
                            "[Bars] Freshness check | required=False market_is_open=%s "
                            "status=not_enforced symbol_count=%s max_age_minutes=%s",
                            market_is_open,
                            len(symbols or []),
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

                        continue

                    if not market_is_open:
                        layers = app_state.setdefault("layers", {})
                        layers["last_off_hours_cycle"] = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "reason": "market_closed_observation_only",
                            "symbols": list(symbols),
                            "bar_counts": bar_counts,
                            "freshness_report": freshness_report,
                        }

                        logging.info(
                            "[Layers] Market closed; completed observation-only bar/freshness cycle. "
                            "Skipping Layer 1/2 target update, Layer 3 planning, and Layer 4 execution."
                        )

                        _expire_active_plan_for_market_close()

                        continue

                    evaluation_symbols = list(fresh_bars_by_symbol.keys())

                    result = layer_engine.evaluate(
                        evaluation_symbols,
                        bars_by_symbol=fresh_bars_by_symbol,
                    )

                    logging.info("[Layers] Evaluation complete.")

                    ranked = result.get("ranked", [])
                    target = result.get("target_portfolio", {})

                    logging.info(
                        "[Layers] Ranked count=%s target_summary=%s",
                        len(ranked),
                        _target_summary_for_log(target),
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
                        "open_orders=%s fail_safe_active=%s",
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
                    )

                    layer4_execution_result = execute_layer4_plan(
                        layer3_plan,
                        layer3_summary,
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
                            _target_summary_for_log(target),
                        )

        except asyncio.CancelledError:
            logging.info("[Layers] Layer monitor cancelled.")
            raise

        except Exception:
            logging.exception("[Layers] Layer monitor evaluation failed.")

        finally:
            if not app_state["stream"]["shutdown_event"].is_set():
                await _sleep_until_next_layer_boundary(interval_seconds)

    logging.info("[Layers] Layer monitor exited cleanly.")