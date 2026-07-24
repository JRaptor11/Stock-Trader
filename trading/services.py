import asyncio
import time
import logging
from datetime import datetime
from runners.heartbeat import Heartbeat
from core.state import app_state


BALANCE_ACCOUNT_TIMEOUT_SECONDS = 10.0
BALANCE_FAILURE_BACKOFF_MAX_SECONDS = 120.0


async def _sleep_with_shutdown(seconds: float, step: float = 0.25) -> None:
    """
    Async sleep that exits early if app_state["stream"]["shutdown_event"] is set.
    """
    end = time.time() + float(seconds)
    while time.time() < end:
        if app_state["stream"]["shutdown_event"].is_set():
            return
        await asyncio.sleep(min(step, end - time.time()))


def _wait_with_shutdown(seconds: float) -> bool:
    """
    Sync wait that exits early if shutdown is requested.
    Returns True if shutdown was set during the wait.
    """
    return app_state["stream"]["shutdown_event"].wait(timeout=float(seconds))


class PositionTracker:
    """
    Continuously tracks open positions with thread-safe access.
    """
    def __init__(self, trading_client):
        self.trading_client = trading_client
        self._heartbeat = Heartbeat()
        app_state["services"]["position_tracker"]["instance"] = self

    async def update_positions(self):
        """
        Background loop to fetch and update current positions.
        Cleanly exits when task is cancelled during shutdown.
        """
        app_state['services']["position_tracker"]["running"] = True
        logging.info("[PositionTracker] Starting periodic position updates.")

        try:
            while app_state['services']["position_tracker"]["running"]:
                self._heartbeat.beat()
                try:
                    positions = self.trading_client.get_all_positions()
                    with app_state['services']["position_tracker"]["lock"]:
                        app_state['services']["position_tracker"]["positions"] = {
                            p.symbol: p for p in positions
                        }
                    await _sleep_with_shutdown(15)

                except asyncio.CancelledError:
                    logging.info("[PositionTracker] Update task cancelled.")
                    raise

                except Exception as e:
                    logging.error(f"[PositionTracker] ❌ Failed to update positions: {e}")
                    await _sleep_with_shutdown(30)

        except asyncio.CancelledError:
            # Let cancellation propagate, but log nicely and cleanup in finally
            raise

        finally:
            app_state['services']["position_tracker"]["running"] = False
            logging.info("[PositionTracker] Position update loop exited cleanly.")

    def get_position(self, symbol):
        """
        Thread-safe access to a specific position.
        """
        with app_state["services"]["position_tracker"]["lock"]:
            return app_state["services"]["position_tracker"]["positions"].get(symbol)

    def stop(self):
        """
        Stop the position update loop.
        """
        app_state["services"]["position_tracker"]["running"] = False


class PerformanceMonitor:
    """
    Tracks maximum drawdown based on balance and equity.
    """
    def __init__(self, balance_tracker):
        self.balance_tracker = balance_tracker
        self._heartbeat = Heartbeat()
        app_state["services"]["performance_monitor"]["instance"] = self

    def track(self):
        """
        Background loop to periodically update drawdown.
        Stops cleanly on shutdown_event.
        """
        while not app_state["stream"]["shutdown_event"].is_set():
            self._heartbeat.beat()
            try:
                data = self.balance_tracker.get_balance()
                with app_state["services"]["performance_monitor"]["lock"]:
                    equity = data["equity"]
                    balance = data["balance"]
                    drawdown = equity - balance
                    app_state["services"]["performance_monitor"]["max_drawdown"] = min(
                        app_state["services"]["performance_monitor"]["max_drawdown"], drawdown
                    )
                logging.info(f"[PerformanceMonitor] Equity: ${equity:.2f}")
            except Exception as e:
                logging.error(f"[PerformanceMonitor] ❌ Failed to track performance: {e}")

            # Interruptible wait (replaces time.sleep(60))
            if _wait_with_shutdown(60):
                break

        logging.info("[PerformanceMonitor] ✅ Exiting performance monitor loop.")


class OrderExecutor:
    """
    Handles queued order execution with retry logic.
    """
    def __init__(self):
        self._heartbeat = Heartbeat()
        app_state["services"]["order_executor"]["instance"] = self

    async def process_queue(self):
        """
        Background loop to process order queue.
        Stops cleanly on shutdown_event.
        """
        while not app_state["stream"]["shutdown_event"].is_set():
            self._heartbeat.beat()
            order = None
            try:
                # Make queue get interruptible by using a timeout
                try:
                    order = await asyncio.wait_for(
                        app_state["services"]["order_executor"]["queue"].get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue  # re-check shutdown_event

                with app_state["services"]["order_executor"]["lock"]:
                    await self._process_order_internal(order)

            except Exception as e:
                logging.error(f"[OrderExecutor] ❌ Order failed: {e}")
                try:
                    with app_state["services"]["order_executor"]["lock"]:
                        app_state["services"]["order_executor"]["queue"].put_nowait(order)
                except Exception:
                    pass
                await _sleep_with_shutdown(10)

        logging.info("[OrderExecutor] ✅ Exiting order executor loop.")

    async def _process_order_internal(self, order):
        """
        Actual order execution logic. Assumes order is safe to process.
        """
        logging.info(f"[OrderExecutor] 🔄 Processing order: {order}")
        # Actual execution logic should go here


class MarketHoursMonitor:
    """
    Tracks market open/closed status.
    """
    def __init__(self, trading_client):
        self.trading_client = trading_client
        self._heartbeat = Heartbeat()
        app_state["services"]["market_monitor"]["instance"] = self

    def run(self):
        """
        Background loop to periodically check market status.
        Stops cleanly on shutdown_event.
        """
        while not app_state["stream"]["shutdown_event"].is_set():
            self._heartbeat.beat()
            try:
                clock = self.trading_client.get_clock()
                with app_state["services"]["market_monitor"]["lock"]:
                    app_state["services"]["market_monitor"]["market_open"] = clock.is_open
            except Exception as e:
                logging.error(f"[MarketHoursMonitor] Market check failed: {e}")

            # Interruptible wait (replaces time.sleep(300))
            if _wait_with_shutdown(300):
                break

        logging.info("[MarketHoursMonitor] ✅ Exiting market hours monitor loop.")

    def is_market_open(self):
        """
        Thread-safe access to market open status.
        """
        with app_state["services"]["market_monitor"]["lock"]:
            return app_state["services"]["market_monitor"]["market_open"]


class AccountBalanceTracker:
    """
    Thread-safe tracking of account balance and equity.
    """
    def __init__(self, trading_client):
        self.trading_client = trading_client
        self._heartbeat = Heartbeat()

        # Retain an account request that continues running after an asyncio
        # timeout. This prevents repeated timed-out requests from piling up.
        self._account_fetch_task = None

        app_state["services"]["balance_tracker"]["instance"] = self

    def _finish_account_fetch_task(self, task: asyncio.Task) -> None:
        """
        Clean up a broker account-fetch task after it finishes.

        A timed-out asyncio wait cannot forcibly stop the underlying synchronous
        SDK call. Retaining the task prevents another request from starting until
        the original call has actually returned.
        """
        if self._account_fetch_task is task:
            self._account_fetch_task = None

        try:
            task.result()
        except asyncio.CancelledError:
            logging.debug(
                "[BalanceTracker] Broker account-fetch task was cancelled."
            )
        except Exception as exc:
            # The active update call reports the primary failure. This consumes
            # any later task exception without producing an unhandled-task warning.
            logging.debug(
                "[BalanceTracker] Broker account-fetch task completed "
                "with error: %s",
                exc,
            )

    async def update_balance(self):
        """
        Fetch and update balance and equity information without blocking the
        asyncio event loop on the synchronous Alpaca SDK request.
        """
        self._heartbeat.beat()

        existing_task = self._account_fetch_task

        if existing_task is not None and not existing_task.done():
            raise TimeoutError(
                "Previous broker account request is still running; "
                "skipping overlapping request."
            )

        fetch_task = asyncio.create_task(
            asyncio.to_thread(self.trading_client.get_account),
            name="balance-tracker-get-account",
        )

        self._account_fetch_task = fetch_task
        fetch_task.add_done_callback(self._finish_account_fetch_task)

        try:
            # Shield prevents wait_for() from cancelling the retained worker task.
            # The underlying synchronous SDK request may still finish later.
            account = await asyncio.wait_for(
                asyncio.shield(fetch_task),
                timeout=BALANCE_ACCOUNT_TIMEOUT_SECONDS,
            )

        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                "Broker account request exceeded "
                f"{BALANCE_ACCOUNT_TIMEOUT_SECONDS:.1f} seconds."
            ) from exc

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            raise RuntimeError(
                f"Broker account request failed: {exc}"
            ) from exc

        # Hold the state lock only while copying completed data. Never hold it
        # during broker network activity.
        cash_value = float(account.cash)
        equity_value = float(account.equity)

        with app_state["services"]["balance_tracker"]["lock"]:
            app_state["services"]["balance_tracker"]["balance"] = (
                cash_value
            )
            app_state["services"]["balance_tracker"]["equity"] = (
                equity_value
            )
            app_state["services"]["balance_tracker"]["last_updated"] = (
                datetime.now()
            )

        app_state["equity"] = equity_value

        main_state = app_state.setdefault("main", {})

        if (
            equity_value > 0
            and not float(main_state.get("starting_equity") or 0.0)
        ):
            main_state["starting_equity"] = equity_value
            logging.info(
                "💰 Starting equity recorded after successful "
                "balance update: $%.2f",
                equity_value,
            )

        self._heartbeat.beat()

    def get_balance(self):
        """
        Thread-safe access to current balance and equity.
        """
        with app_state["services"]["balance_tracker"]["lock"]:
            return {
                "balance": app_state["services"]["balance_tracker"]["balance"],
                "equity": app_state["services"]["balance_tracker"]["equity"],
                "updated": app_state["services"]["balance_tracker"]["last_updated"],
            }

    async def start_periodic_updates(self, interval_seconds=30):
        """
        Continuously update account balance and push equity to app_state.

        Broker failures use bounded exponential backoff, while successful
        requests restore the normal update interval.
        """
        tracker_state = app_state["services"]["balance_tracker"]

        if tracker_state["running"]:
            logging.info(
                "[BalanceTracker] Periodic update already running."
            )
            return

        tracker_state["running"] = True
        consecutive_failures = 0

        logging.info(
            "[BalanceTracker] Starting periodic account balance updates "
            "| interval_seconds=%s timeout_seconds=%.1f "
            "max_backoff_seconds=%.1f",
            interval_seconds,
            BALANCE_ACCOUNT_TIMEOUT_SECONDS,
            BALANCE_FAILURE_BACKOFF_MAX_SECONDS,
        )

        try:
            while (
                tracker_state["running"]
                and not app_state["stream"]["shutdown_event"].is_set()
            ):
                sleep_seconds = float(interval_seconds)

                try:
                    await self.update_balance()

                    balance_snapshot = self.get_balance()
                    app_state["equity"] = balance_snapshot["equity"]

                    if consecutive_failures:
                        logging.info(
                            "[BalanceTracker] Broker account updates recovered "
                            "| previous_consecutive_failures=%d",
                            consecutive_failures,
                        )

                    consecutive_failures = 0

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    consecutive_failures += 1

                    # 30-second normal interval becomes 60 seconds after the
                    # first failure and 120 seconds after subsequent failures.
                    sleep_seconds = min(
                        float(interval_seconds)
                        * (2 ** min(consecutive_failures, 2)),
                        BALANCE_FAILURE_BACKOFF_MAX_SECONDS,
                    )

                    logging.error(
                        "[BalanceTracker] Periodic update failed "
                        "| consecutive_failures=%d "
                        "next_retry_seconds=%.1f error=%s",
                        consecutive_failures,
                        sleep_seconds,
                        exc,
                    )

                await asyncio.sleep(sleep_seconds)

        except asyncio.CancelledError:
            logging.info(
                "[BalanceTracker] Periodic update task cancelled."
            )
            raise

        finally:
            tracker_state["running"] = False
            logging.info(
                "[BalanceTracker] Periodic update loop exited cleanly."
            )

    def stop_periodic_updates(self):
        """
        Stop the periodic balance update loop.
        """
        app_state["services"]["balance_tracker"]["running"] = False