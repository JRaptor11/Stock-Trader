import asyncio
import logging
from datetime import datetime, timezone
from bar_data import fetch_recent_bars

from state import app_state


async def run_layer_monitor(interval_seconds: int = 900) -> None:
    """
    Runs Layer 1/2 evaluation on a timer.

    This does NOT place trades.
    It only logs rankings and target portfolio.
    Safe during off-hours because it does not depend on incoming trade ticks.
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

                    bars_by_symbol = fetch_recent_bars(
                        app_state.get("stock_data_client"),
                        symbols,
                        lookback_hours=48,
                        timeframe_minutes=15,
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

                    result = layer_engine.evaluate(
                        symbols,
                        bars_by_symbol=bars_by_symbol,
                    )

                    logging.info("[Layers] Evaluation complete.")

                    ranked = result.get("ranked", [])
                    target = result.get("target_portfolio", {})

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