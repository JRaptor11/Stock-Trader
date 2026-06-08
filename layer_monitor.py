import asyncio
import logging
from datetime import datetime, timezone

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
                    
                    result = layer_engine.evaluate(symbols)

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