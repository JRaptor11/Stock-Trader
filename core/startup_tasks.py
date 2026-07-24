# startup_tasks.py

import os
import time
import logging
import asyncio
from datetime import datetime
from collections import deque

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient

from core.state import app_state
from safety.fail_safes import monitor_fail_safes
from trading.services import (
    PositionTracker,
    OrderExecutor,
    AccountBalanceTracker,
)
from market.stream import ThreadedAlpacaStream
from strategies.strategy import AtrNoiseFilter, VolatilityScorer

from runners.layer_monitor import run_layer_monitor
from core.app_state_init import initialize_layer_state
from trading.portfolio_reconciler import run_portfolio_reconciler

from config import runtime_config as config
from utils.threading_utils import safe_thread
from utils.system_utils import monitor_system_resources
from trading.trade_utils import ensure_trade_logs_exist
from utils.lifecycle_utils import (
    record_program_startup,
    was_last_program_shutdown_abnormal,
    sync_open_positions_to_app_state,
    safe_send_startup_alert,
)
from integrations.telegram_bot import start_telegram_bot


_startup_alert_tasks: set[asyncio.Task] = set()


def _finish_startup_alert_task(task: asyncio.Task) -> None:
    """
    Retain detached startup-alert tasks until completion and consume their
    result so failures never become unhandled-task warnings.
    """
    _startup_alert_tasks.discard(task)

    try:
        task.result()
    except asyncio.CancelledError:
        logging.info("[StartupAlert] Startup alert task was cancelled.")
    except Exception:
        logging.exception("[StartupAlert] Detached startup alert failed.")


async def background_startup_after_bind() -> None:
    """
    Finish slow startup work after the app has already started.

    This keeps Render port detection fast while still initializing
    Alpaca clients, services, layer monitors, stream, fail-safes,
    portfolio reconciler, and Telegram.
    """
    try:
        await asyncio.sleep(2)

        if app_state["stream"]["shutdown_event"].is_set():
            logging.info("[StartupBg] Shutdown detected before background startup. Aborting.")
            return

        abnormal, reason = was_last_program_shutdown_abnormal()
        if abnormal:
            alert_task = asyncio.create_task(
                safe_send_startup_alert(
                    "⚠️ Abnormal Program Shutdown Detected",
                    f"Previous shutdown was not clean: {reason}",
                ),
                name="startup-abnormal-shutdown-alert",
            )

            _startup_alert_tasks.add(alert_task)
            alert_task.add_done_callback(_finish_startup_alert_task)

            logging.info(
                "[StartupAlert] Abnormal-shutdown alert scheduled without "
                "blocking background startup."
            )

        app_state["trading_client"] = TradingClient(
            config.API_KEY,
            config.SECRET_KEY,
            paper=True,
        )
        app_state["stock_data_client"] = StockHistoricalDataClient(
            config.API_KEY,
            config.SECRET_KEY,
        )

        if not callable(app_state["trading_client"].get_all_positions) or not callable(
            app_state["trading_client"].submit_order
        ):
            raise RuntimeError("❌ Trading client methods appear to be overwritten or invalid.")

        try:
            clock = await asyncio.to_thread(
                app_state["trading_client"].get_clock
            )
            app_state["market_open_time"] = clock.next_open.timestamp()
            logging.info(
                "🕒 Market opens at: %s",
                datetime.fromtimestamp(app_state["market_open_time"]),
            )
        except Exception as e:
            logging.warning("⚠️ Could not fetch market open time: %s", e)
            app_state["market_open_time"] = time.time()

        previous_thread = app_state["stream"].get("thread")
        if previous_thread and previous_thread.is_alive():
            logging.info("⏳ Waiting for previous stream thread to fully stop...")

            loop = asyncio.get_running_loop()
            start = time.time()

            def _join_previous_thread():
                previous_thread.join(timeout=30)
                return not previous_thread.is_alive()

            try:
                stopped = await asyncio.wait_for(
                    loop.run_in_executor(None, _join_previous_thread),
                    timeout=35,
                )
                if not stopped:
                    logging.warning("⚠️ Previous stream thread is still alive after join timeout.")
                else:
                    logging.info("✅ Previous stream thread fully stopped.")
            except asyncio.TimeoutError:
                logging.warning("⏱ Timed out waiting for previous stream thread to stop.")

            elapsed = time.time() - start
            if elapsed > 10:
                logging.warning("⚠️ Previous stream shutdown wait took %.2f seconds.", elapsed)
            else:
                logging.info("✅ Previous stream shutdown wait completed in %.2f seconds.", elapsed)

        app_state["services"]["position_tracker"]["instance"] = PositionTracker(
            app_state["trading_client"]
        )
        app_state["services"]["balance_tracker"]["instance"] = AccountBalanceTracker(
            app_state["trading_client"]
        )
        app_state["services"]["order_executor"]["instance"] = OrderExecutor()

        app_state["strategy"]["recent_prices"] = deque(maxlen=100)
        app_state["strategy"]["atr_filter"] = AtrNoiseFilter(period=14)
        app_state["strategy"]["volatility_scorer"] = VolatilityScorer()

        # ─────────────────────────────────────────────
        # Layered portfolio architecture
        # ─────────────────────────────────────────────
        initialize_layer_state(top_n=5)

        layer_task = asyncio.create_task(
            run_layer_monitor(),
            name="layer-monitor-task",
        )
        app_state["main"]["async_tasks"].add(layer_task)

        logging.info("[Layers] Layer engine initialized and monitor task scheduled.")

        # Start Alpaca stream after layer monitor is scheduled.
        app_state["stream"]["manager"] = ThreadedAlpacaStream(
            config.API_KEY,
            config.SECRET_KEY,
            app_state["main"]["symbol"],
        )
        stream = app_state["stream"]["manager"]
        stream.start()

        position_task = asyncio.create_task(
            app_state["services"]["position_tracker"]["instance"].update_positions(),
            name="position-tracker-task",
        )
        app_state["main"]["async_tasks"].add(position_task)

        balance_tracker = (
            app_state["services"]["balance_tracker"]["instance"]
        )

        try:
            await balance_tracker.update_balance()

            balance_snapshot = balance_tracker.get_balance()
            current_equity = float(
                balance_snapshot.get("equity") or 0.0
            )

            app_state["equity"] = current_equity

            if current_equity > 0:
                app_state["main"]["starting_equity"] = current_equity
                logging.info(
                    "💰 Starting equity recorded from balance tracker: $%.2f",
                    current_equity,
                )
            else:
                logging.warning(
                    "⚠️ Initial balance update returned no usable equity."
                )

        except Exception:
            logging.warning(
                "⚠️ Initial balance update failed; periodic updates will retry.",
                exc_info=True,
            )

        # Start the periodic loop only after the one-time startup request has
        # completed or failed. This prevents two simultaneous account requests.
        balance_task = asyncio.create_task(
            balance_tracker.start_periodic_updates(),
            name="balance-tracker-task",
        )
        app_state["main"]["async_tasks"].add(balance_task)

        ensure_trade_logs_exist()
        record_program_startup()

        try:
            await asyncio.to_thread(
                sync_open_positions_to_app_state,
                app_state,
            )
        except Exception:
            logging.warning(
                "⚠️ Initial position sync failed (startup continues).",
                exc_info=True,
            )

        portfolio_reconcile_task = asyncio.create_task(
            run_portfolio_reconciler(interval_seconds=300, repair=True),
            name="portfolio-reconciler-task",
        )
        app_state["main"]["async_tasks"].add(portfolio_reconcile_task)

        logging.info("[PortfolioReconcile] Portfolio reconciler task scheduled.")

        t1 = safe_thread(
            monitor_fail_safes,
            name="FailSafeMonitor",
            daemon=True,
        )
        app_state["main"]["threads"].append(t1)

        if os.getenv("ENV", "development") != "production":
            t2 = safe_thread(
                monitor_system_resources,
                name="ResourceMonitor",
                daemon=True,
            )
            app_state["main"]["threads"].append(t2)

        start_telegram_bot()

        logging.info("✅ Background startup complete.")

    except asyncio.CancelledError:
        logging.info("[StartupBg] Background startup task cancelled.")
        raise

    except Exception:
        logging.exception("❌ Background startup failed.")