import asyncio
import time
import logging
from datetime import datetime
from heartbeat import Heartbeat
from state import app_state


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
        app_state["services"]["balance_tracker"]["instance"] = self

    async def update_balance(self):
        """
        Fetch and update balance and equity information.
        """
        self._heartbeat.beat()
        try:
            with app_state["services"]["balance_tracker"]["lock"]:
                account = self.trading_client.get_account()
                app_state["services"]["balance_tracker"]["balance"] = float(account.cash)
                app_state["services"]["balance_tracker"]["equity"] = float(account.equity)
                app_state["services"]["balance_tracker"]["last_updated"] = datetime.now()
            self._heartbeat.beat()
        except Exception as e:
            logging.error(f"[BalanceTracker] Balance update failed: {e}")
            raise

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
        Handles graceful shutdown when task is cancelled.
        """

        if app_state['services']["balance_tracker"]["running"]:
            logging.info("[BalanceTracker] Periodic update already running.")
            return

        app_state['services']["balance_tracker"]["running"] = True
        logging.info("[BalanceTracker] Starting periodic account balance updates.")

        try:
            while app_state['services']["balance_tracker"]["running"] and not app_state["stream"]["shutdown_event"].is_set():
                try:
                    await self.update_balance()
                    app_state["equity"] = self.get_balance()["equity"]
                except Exception as e:
                    logging.error(f"[BalanceTracker] Periodic update failed: {e}")

                await asyncio.sleep(interval_seconds)

        except asyncio.CancelledError:
            logging.info("[BalanceTracker] Periodic update task cancelled.")
            raise

        finally:
            app_state['services']["balance_tracker"]["running"] = False
            logging.info("[BalanceTracker] Periodic update loop exited cleanly.")

def stop_periodic_updates(self):
    """
    Stop the periodic balance update loop.
    """
    app_state["services"]["balance_tracker"]["running"] = False