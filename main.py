# main.py
# This version is modified to use uvicorn instead of gunicorn + gevent
# Uvicorn allows direct async handling and works well for local or dev environments

import os
import time
import logging
import asyncio
import inspect
import threading
from datetime import datetime
from contextlib import asynccontextmanager
from collections import deque

from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from app_instance import app
from routes.auth_routes import auth_routes
from routes.admin_routes import admin_routes
from routes.dev_routes import dev_routes
from routes.public_routes import public_routes
from state import app_state
from fail_safes import monitor_fail_safes
from services import (
    PositionTracker,
    OrderExecutor,
    AccountBalanceTracker,
)
from stream import ThreadedAlpacaStream
from utils.threading_utils import safe_thread
from utils.system_utils import monitor_system_resources
from utils.trade_utils import ensure_trade_logs_exist
from utils.lifecycle_utils import (
    record_program_startup,
    record_program_shutdown,
    was_last_program_shutdown_abnormal,
    sync_open_positions_to_app_state,
)
from utils.logging_utils import configure_logging, handle_asyncio_exception
from utils.alerts_utils import send_email_alert
from utils.telegram_bot_utils import start_telegram_bot

from utils import config_utils as config
from constants import (
    TRADE_WINDOW,
    TRADE_LIMIT,
    TRADE_COOLDOWN,
    EQUITY_THRESHOLD,
    EQUITY_FAILSAFE_COOLDOWN,
    MAX_POSITION_LOSS_PERCENT,
    MAX_EQUITY_LOSS,
    MAX_POSITION_LOSS,
    MAX_CONNECTION_ERRORS,
    CONNECTION_COOLDOWN,
    BUY_ORDER_THROTTLE_SECONDS,
    MIN_ORDER_AGE_SECONDS,
    TRADE_RATE_RESPONSE,
    MIN_REENTRY_CHANGE_PCT,
    BUY_CONFIDENCE_THRESHOLD,
    SELL_CONFIDENCE_THRESHOLD,
    CONFIDENCE_CONFLICT_MARGIN,
    MEMORY_ALERT_MB,
    CPU_ALERT_PERCENT,
    TRADE_SUMMARY_FILE,
    TRADE_HISTORY_FILE,
)

from config import (
    PROGRAM_STARTUP_FILE,
    PROGRAM_SHUTDOWN_FILE,
    PROGRAM_SHUTDOWN_REASON_FILE,
)

from strategy import AtrNoiseFilter, VolatilityScorer
from alpaca.trading.client import TradingClient

load_dotenv()


def ensure_app_state_structure() -> None:
    """Ensure expected nested dicts/containers exist to prevent KeyErrors."""

    main = app_state.setdefault("main", {})
    main.setdefault("symbol", [])
    main.setdefault("async_tasks", set())
    main.setdefault("services", {})
    main.setdefault("starting_equity", None)
    main.setdefault("threads", [])
    main.setdefault("startup_background_task", None)

    app_state.setdefault("paths", {})
    app_state.setdefault("secrets", {})
    app_state.setdefault("log_level", {})

    stream = app_state.setdefault("stream", {})
    stream.setdefault("manager", None)
    stream.setdefault("instance", None)
    stream.setdefault("shutdown_event", threading.Event())
    stream.setdefault("running", False)
    stream.setdefault("stopping", False)
    stream.setdefault("state", "stopped")
    stream.setdefault("loop", None)
    stream.setdefault("thread", None)
    stream.setdefault("lock", threading.Lock())

    debug = stream.setdefault("debug", {})
    debug.setdefault("status", "init")
    debug.setdefault("last_restart", None)
    debug.setdefault("last_trade", None)

    services = app_state.setdefault("services", {})
    services.setdefault("position_tracker", {})
    services.setdefault("balance_tracker", {})
    services.setdefault("order_executor", {})

    fail_safes = app_state.setdefault("fail_safes", {})
    fail_safes.setdefault("state", False)
    fail_safes.setdefault("position_lock", None)
    fail_safes.setdefault("invalid_price_cache", {})
    fail_safes.setdefault("liquidation_in_progress", set())
    fail_safes.setdefault("symbols", set())

    strategy = app_state.setdefault("strategy", {})
    strategy.setdefault("sells_in_progress", set())
    strategy.setdefault("recent_prices", deque(maxlen=100))
    strategy.setdefault("atr_filter", None)
    strategy.setdefault("volatility_scorer", None)

    app_state.setdefault("open_trades", {})

    telegram = app_state.setdefault("telegram", {})
    telegram.setdefault("bot_started", False)
    telegram.setdefault("bot_app", None)
    telegram.setdefault("task", None)
    telegram.setdefault("handle", None)


def load_environment_config() -> None:
    """Load environment variables into the shared config module."""

    config.API_KEY = os.getenv("API_KEY")
    config.SECRET_KEY = os.getenv("SECRET_KEY")
    config.ALPACA_URL = os.getenv("ALPACA_URL")

    config.EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
    config.EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
    config.EMAIL_RECIPIENTS = [
        e.strip() for e in os.getenv("EMAIL_RECIPIENTS", "").split(",") if e.strip()
    ]

    config.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not config.TELEGRAM_BOT_TOKEN:
        logging.error("❌ TELEGRAM_BOT_TOKEN is not set!")

    chat_ids_str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if chat_ids_str:
        try:
            config.TELEGRAM_CHAT_ID = [
                int(cid.strip()) for cid in chat_ids_str.split(",") if cid.strip()
            ]
            logging.info(f"✅ Loaded Telegram chat IDs: {config.TELEGRAM_CHAT_ID}")
        except ValueError:
            config.TELEGRAM_CHAT_ID = []
            raise RuntimeError(
                "TELEGRAM_CHAT_ID must be an integer or comma-separated integers (e.g., 123 or 123,456)."
            )
    else:
        config.TELEGRAM_CHAT_ID = []

    if config.TELEGRAM_BOT_TOKEN and not config.TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TELEGRAM_BOT_TOKEN is set but TELEGRAM_CHAT_ID is missing/empty.")

    config.HEALTH_USERNAME = os.getenv("HEALTH_USERNAME")
    config.HEALTH_PASSWORD = os.getenv("HEALTH_PASSWORD")

    if not config.HEALTH_USERNAME or not config.HEALTH_PASSWORD:
        raise RuntimeError("HEALTH_USERNAME / HEALTH_PASSWORD must be set")

    config.ENABLE_DEV_ROUTES = os.getenv("ENABLE_DEV_ROUTES", "false").lower() == "true"

async def safe_close_trading_client(client) -> None:
    """Close the trading client safely whether close() is sync or async."""
    close_fn = getattr(client, "close", None)
    if not callable(close_fn):
        return

    try:
        result = close_fn()
        if asyncio.iscoroutine(result):
            await result
    except Exception as e:
        logging.warning(f"Error closing trading client: {e}")


async def _safe_send_startup_alert(subject: str, body: str) -> None:
    """Run blocking email send off the event loop and never block startup."""
    try:
        await asyncio.to_thread(send_email_alert, subject, body)
    except Exception:
        logging.warning("Failed sending startup alert (ignored).", exc_info=True)


async def _background_startup_after_bind() -> None:
    """
    Finish slow startup work after the app has already started.
    This keeps Render port detection fast.
    """
    try:
        # Small delay so Uvicorn can finish startup and Render can detect the port
        await asyncio.sleep(2)

        if app_state["stream"]["shutdown_event"].is_set():
            logging.info("[StartupBg] Shutdown detected before background startup. Aborting.")
            return

        abnormal, reason = was_last_program_shutdown_abnormal()
        if abnormal:
            await _safe_send_startup_alert(
                "⚠️ Abnormal Program Shutdown Detected",
                f"Previous shutdown was not clean: {reason}",
            )

        app_state["trading_client"] = TradingClient(config.API_KEY, config.SECRET_KEY, paper=True)

        if not callable(app_state["trading_client"].get_all_positions) or not callable(
            app_state["trading_client"].submit_order
        ):
            raise RuntimeError("❌ Trading client methods appear to be overwritten or invalid.")

        try:
            clock = app_state["trading_client"].get_clock()
            app_state["market_open_time"] = clock.next_open.timestamp()
            logging.info(f"🕒 Market opens at: {datetime.fromtimestamp(app_state['market_open_time'])}")
        except Exception as e:
            logging.warning(f"⚠️ Could not fetch market open time: {e}")
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
                logging.warning(f"⚠️ Previous stream shutdown wait took {elapsed:.2f} seconds.")
            else:
                logging.info(f"✅ Previous stream shutdown wait completed in {elapsed:.2f} seconds.")

        app_state["services"]["position_tracker"]["instance"] = PositionTracker(app_state["trading_client"])
        app_state["services"]["balance_tracker"]["instance"] = AccountBalanceTracker(app_state["trading_client"])
        app_state["services"]["order_executor"]["instance"] = OrderExecutor()

        app_state["strategy"]["recent_prices"] = deque(maxlen=100)
        app_state["strategy"]["atr_filter"] = AtrNoiseFilter(period=14)
        app_state["strategy"]["volatility_scorer"] = VolatilityScorer()

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
        balance_task = asyncio.create_task(
            app_state["services"]["balance_tracker"]["instance"].start_periodic_updates(),
            name="balance-tracker-task",
        )
        app_state["main"]["async_tasks"].update({position_task, balance_task})

        try:
            await app_state["services"]["balance_tracker"]["instance"].update_balance()
        except Exception:
            logging.warning("⚠️ Initial balance update failed (startup continues).", exc_info=True)

        try:
            account = app_state["trading_client"].get_account()
            app_state["main"]["starting_equity"] = float(account.equity)
            logging.info(f"💰 Starting equity recorded: ${app_state['main']['starting_equity']:.2f}")
        except Exception as e:
            logging.error(f"⚠️ Failed to retrieve starting equity: {e}")

        ensure_trade_logs_exist()
        record_program_startup()

        try:
            sync_open_positions_to_app_state(app_state)
        except Exception:
            logging.warning("⚠️ Initial position sync failed (startup continues).", exc_info=True)

        t1 = safe_thread(monitor_fail_safes, name="FailSafeMonitor", daemon=True)
        app_state["main"]["threads"].append(t1)

        if os.getenv("ENV", "development") != "production":
            t2 = safe_thread(monitor_system_resources, name="ResourceMonitor", daemon=True)
            app_state["main"]["threads"].append(t2)

        start_telegram_bot()

        logging.info("✅ Background startup complete.")

    except asyncio.CancelledError:
        logging.info("[StartupBg] Background startup task cancelled.")
        raise
    except Exception:
        logging.exception("❌ Background startup failed.")


@asynccontextmanager
async def lifespan(app_fastapi):
    """Application startup and shutdown logic for the trading bot."""

    app_state["startup_time"] = time.time()

    configure_logging()
    asyncio.get_running_loop().set_exception_handler(handle_asyncio_exception)
    ensure_app_state_structure()

    try:
        logging.info("🚀 Initializing trading bot...")

        # Always reset shutdown event for a fresh process start
        app_state["stream"]["shutdown_event"].clear()

        load_environment_config()

        if app_state["fail_safes"].get("position_lock") is None:
            app_state["fail_safes"]["position_lock"] = asyncio.Lock()

        symbol_raw = os.environ.get("SYMBOL", "AAPL").strip()
        symbols = [s.strip() for s in symbol_raw.split(",") if s.strip()]
        app_state["main"]["symbol"] = symbols
        if not app_state["main"]["symbol"]:
            raise RuntimeError("Symbol is not configured. Bot cannot proceed.")

        app_state["paths"] = {
            "TRADE_SUMMARY_FILE": TRADE_SUMMARY_FILE,
            "TRADE_HISTORY_FILE": TRADE_HISTORY_FILE,
            "PROGRAM_STARTUP_FILE": PROGRAM_STARTUP_FILE,
            "PROGRAM_SHUTDOWN_FILE": PROGRAM_SHUTDOWN_FILE,
            "PROGRAM_SHUTDOWN_REASON_FILE": PROGRAM_SHUTDOWN_REASON_FILE,
        }

        app_state["config_defaults"] = {
            "TRADE_WINDOW": TRADE_WINDOW,
            "TRADE_LIMIT": TRADE_LIMIT,
            "TRADE_COOLDOWN": TRADE_COOLDOWN,
            "BUY_ORDER_THROTTLE_SECONDS": BUY_ORDER_THROTTLE_SECONDS,
            "MIN_ORDER_AGE_SECONDS": MIN_ORDER_AGE_SECONDS,
            "TRADE_RATE_RESPONSE": TRADE_RATE_RESPONSE,
            "MIN_REENTRY_CHANGE_PCT": MIN_REENTRY_CHANGE_PCT,
            "BUY_CONFIDENCE_THRESHOLD": BUY_CONFIDENCE_THRESHOLD,
            "SELL_CONFIDENCE_THRESHOLD": SELL_CONFIDENCE_THRESHOLD,
            "CONFIDENCE_CONFLICT_MARGIN": CONFIDENCE_CONFLICT_MARGIN,
            "EQUITY_THRESHOLD": EQUITY_THRESHOLD,
            "EQUITY_FAILSAFE_COOLDOWN": EQUITY_FAILSAFE_COOLDOWN,
            "MAX_POSITION_LOSS_PERCENT": MAX_POSITION_LOSS_PERCENT,
            "MAX_EQUITY_LOSS": MAX_EQUITY_LOSS,
            "MAX_POSITION_LOSS": MAX_POSITION_LOSS,
            "MAX_CONNECTION_ERRORS": MAX_CONNECTION_ERRORS,
            "CONNECTION_COOLDOWN": CONNECTION_COOLDOWN,
            "MEMORY_ALERT_MB": MEMORY_ALERT_MB,
            "CPU_ALERT_PERCENT": CPU_ALERT_PERCENT,
            "HEALTH_USERNAME": config.HEALTH_USERNAME,
            "HEALTH_PASSWORD": config.HEALTH_PASSWORD,
        }

        app_state["fail_safes"]["state"] = False

        app_state["main"]["services"] = {
            "EMAIL_RECIPIENTS": config.EMAIL_RECIPIENTS,
            "ALPACA_URL": config.ALPACA_URL,
            "TELEGRAM_BOT_TOKEN": config.TELEGRAM_BOT_TOKEN,
            "TELEGRAM_CHAT_ID": config.TELEGRAM_CHAT_ID,
        }

        # Startup returns quickly now. Slow work is deferred.
        bg_task = asyncio.create_task(
            _background_startup_after_bind(),
            name="startup-background-task",
        )
        app_state["main"]["startup_background_task"] = bg_task
        app_state["main"]["async_tasks"].add(bg_task)

        yield

    finally:
        try:
            app_state["stream"]["shutdown_event"].set()
        except Exception:
            pass

        try:
            tasks = list(app_state["main"]["async_tasks"])
            for task in tasks:
                task.cancel()

            if tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=10,
                    )
                except asyncio.TimeoutError:
                    logging.warning("⚠️ Timed out waiting for background async tasks to cancel.")

            app_state["main"]["async_tasks"].clear()
        except Exception as e:
            logging.warning(f"⚠️ Error cancelling async tasks: {e}")

        logging.info("⏹ Attempting to stop data stream...")
        stream = app_state["stream"].get("manager")

        if stream:
            try:
                if inspect.iscoroutinefunction(getattr(stream, "stop", None)):
                    await asyncio.wait_for(stream.stop(), timeout=15)
                else:
                    await asyncio.wait_for(asyncio.to_thread(stream.stop), timeout=15)
            except asyncio.TimeoutError:
                logging.warning("⚠️ Stream stop timed out.")
            except Exception as e:
                logging.error(f"❌ Error stopping stream: {e}")
            finally:
                app_state["stream"]["running"] = False

                stream_thread = app_state["stream"].get("thread")
                if not stream_thread or not stream_thread.is_alive():
                    app_state["stream"]["thread"] = None
                    app_state["stream"]["loop"] = None
                    app_state["stream"]["instance"] = None
                    app_state["stream"]["manager"] = None
                    app_state["stream"]["state"] = "stopped"

        try:
            if app_state["services"].get("position_tracker", {}).get("instance"):
                app_state["services"]["position_tracker"]["instance"].stop()
            if app_state["services"].get("balance_tracker", {}).get("instance"):
                app_state["services"]["balance_tracker"]["instance"].stop_periodic_updates()
        except Exception:
            pass

        if app_state.get("trading_client"):
            await safe_close_trading_client(app_state["trading_client"])

        try:
            telegram_state = app_state.get("telegram", {})
            tg_app = telegram_state.get("bot_app")
            tg_task = telegram_state.get("task")

            if tg_app:
                try:
                    if getattr(tg_app, "updater", None):
                        await asyncio.wait_for(tg_app.updater.stop(), timeout=5)
                except Exception:
                    logging.warning("[TelegramBot] updater.stop() failed (ignored).", exc_info=True)

                try:
                    await asyncio.wait_for(tg_app.stop(), timeout=5)
                except Exception:
                    logging.warning("[TelegramBot] app.stop() failed (ignored).", exc_info=True)

                try:
                    await asyncio.wait_for(tg_app.shutdown(), timeout=5)
                except Exception:
                    logging.warning("[TelegramBot] app.shutdown() failed (ignored).", exc_info=True)

            if tg_task:
                tg_task.cancel()
                try:
                    await asyncio.wait_for(tg_task, timeout=5)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logging.warning("[TelegramBot] telegram task cancel/wait failed (ignored).", exc_info=True)

        except Exception:
            logging.warning("Telegram shutdown failed (ignored).", exc_info=True)
        finally:
            app_state["stream"]["running"] = False

            stream_thread = app_state["stream"].get("thread")
            if not stream_thread or not stream_thread.is_alive():
                app_state["stream"]["instance"] = None
                app_state["stream"]["thread"] = None
                app_state["stream"]["loop"] = None
                app_state["stream"]["state"] = "stopped"

        try:
            threads = list(app_state["main"].get("threads", []))
            for t in threads:
                if t and getattr(t, "is_alive", lambda: False)():
                    t.join(timeout=2)
        except Exception:
            logging.warning("⚠️ Error joining background threads (ignored).", exc_info=True)

        app_state["main"]["threads"].clear()

        record_program_shutdown(reason="clean")

        try:
            logging.info("🧵 Threads still alive: " + ", ".join(t.name for t in threading.enumerate()))
        except Exception:
            pass

        try:
            loop = asyncio.get_running_loop()
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            pretty = []
            for t in pending:
                try:
                    name = t.get_name()
                except Exception:
                    name = str(t)
                try:
                    coro = t.get_coro()
                    coro_name = getattr(coro, "__qualname__", repr(coro))
                except Exception:
                    coro_name = "unknown_coro"
                pretty.append(f"{name}->{coro_name}")
            logging.info(f"🌀 Pending asyncio tasks ({len(pending)}): " + " | ".join(pretty[:12]))
        except Exception:
            pass

        logging.info("👋 Shutdown complete")


app.router.lifespan_context = lifespan

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(public_routes, prefix="/api/public", tags=["public"])
app.include_router(auth_routes, prefix="/api/auth", tags=["auth"])
app.include_router(admin_routes, prefix="/api/admin", tags=["admin"])

if getattr(config, "ENABLE_DEV_ROUTES", False):
    app.include_router(dev_routes, prefix="/api/dev", tags=["dev"])

logging.info(f"🔧 ENV is: {os.getenv('ENV', 'development')}")

if __name__ == "__main__":
    import uvicorn

    port_env = os.environ.get("PORT", "8000")
    try:
        port = int(port_env)
    except ValueError:
        logging.warning(f"Invalid PORT env var '{port_env}'. Using default port 8000.")
        port = 8000

    logging.info(f"🚀 Starting FastAPI server on port {port}...")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)