import asyncio
import logging
from datetime import datetime, timezone
from bar_data import fetch_recent_bars_with_min_count

from core.state import app_state
from layer3_rebalancer import run_layer3_dry_run
from layer4_executor import execute_layer4_plan


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
        "[Layers] Stored latest Layer 1/2 result for Layer 3 | ranked_count=%s target=%s",
        len(ranked_snapshot),
        target,
    )


async def run_layer_monitor(interval_seconds: int = 900) -> None:
    """
    Runs Layer 1/2 evaluation on a timer.

    Layer 1/2 builds the target portfolio.
    Layer 3 builds a rebalance plan.

    Order execution is controlled by LAYER3_EXECUTION_ENABLED.
    When disabled, Layer 3 remains dry-run only.
    """
    logging.info("[Layers] Layer monitor started.")

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

                    md = app_state.get("market_data", {}).get("buffer")
                    if md:
                        tick_counts = {
                            symbol: len(md.get_recent_prices(symbol))
                            for symbol in symbols
                        }
                        logging.info("[Layers] Tick counts: %s", tick_counts)

                    bars_by_symbol = fetch_recent_bars_with_min_count(
                        app_state.get("stock_data_client"),
                        symbols,
                        min_bars=180,
                        timeframe_minutes=5,
                        initial_lookback_hours=96,
                        max_lookback_hours=336,
                    )

                    logging.info(
                        "[Layers] Symbols being evaluated: %s",
                        symbols,
                    )

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

                    result = layer_engine.evaluate(
                        symbols,
                        bars_by_symbol=bars_by_symbol,
                    )

                    logging.info("[Layers] Evaluation complete.")

                    ranked = result.get("ranked", [])
                    target = result.get("target_portfolio", {})

                    logging.info(
                        "[Layers] Ranked count=%s Target=%s",
                        len(ranked),
                        target,
                    )

                    store_latest_layer_result(
                        symbols=symbols,
                        bar_counts=bar_counts,
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

                    logging.info("[Layer3] Plan summary: %s", layer3_summary)

                    layer4_execution_result = execute_layer4_plan(
                        layer3_plan,
                        layer3_summary,
                    )

                    logging.info(
                        "[Layer4Exec] Execution result: %s",
                        layer4_execution_result,
                    )

                    if not ranked:
                        logging.info(
                            "[Layers] No ranked symbols yet. Likely not enough market data. "
                            "This is normal during startup/off-hours."
                        )
                    else:
                        top_symbols = [
                            f"{r.symbol}:{r.score:.4f} ({r.reason})"
                            for r in ranked[:5]
                        ]

                        logging.info(
                            "[Layers] Evaluation @ %s | Top Ranked: %s",
                            datetime.now(timezone.utc).isoformat(),
                            top_symbols,
                        )

                        logging.info("[Layers] Target Portfolio: %s", target)

        except asyncio.CancelledError:
            logging.info("[Layers] Layer monitor cancelled.")
            raise

        except Exception:
            logging.exception("[Layers] Layer monitor evaluation failed.")

        # Interruptible sleep so shutdown does not hang.
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    app_state["stream"]["shutdown_event"].wait,
                    interval_seconds,
                ),
                timeout=interval_seconds + 5,
            )
        except asyncio.TimeoutError:
            pass

    logging.info("[Layers] Layer monitor exited cleanly.")