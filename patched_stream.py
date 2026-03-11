# patched_stream.py
import asyncio
import logging
import concurrent.futures
from alpaca.data.live import StockDataStream

class PatchedStockDataStream(StockDataStream):
    async def stop(self):
        """
        Stop the Alpaca websocket regardless of which loop is calling stop().
        NOTE: This helper should NOT log a success message to avoid duplicate shutdown logs.
        The owner (ThreadedAlpacaStream.stop) should log successful shutdown.
        """
        try:
            # If we're already running on the Alpaca stream loop, just await directly.
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None

            if running is getattr(self, "_loop", None):
                await asyncio.wait_for(self.stop_ws(), timeout=10)
            else:
                fut = asyncio.run_coroutine_threadsafe(self.stop_ws(), self._loop)
                await asyncio.wait_for(asyncio.wrap_future(fut), timeout=10)

        except (asyncio.TimeoutError, concurrent.futures.TimeoutError, TimeoutError):
            logging.warning(
                "❌ Alpaca stream shutdown timed out (outer stop may force-exit).",
                exc_info=True,
            )
        except Exception:
            logging.exception("🚨 Unexpected error during Alpaca stream shutdown")