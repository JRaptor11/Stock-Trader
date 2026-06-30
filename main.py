# main.py
# This version is modified to use uvicorn instead of gunicorn + gevent
# Uvicorn allows direct async handling and works well for local or dev environments

import os
import time
import logging
import asyncio
import inspect
import threading
from contextlib import asynccontextmanager

from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from app_instance import app
from routes.auth_routes import auth_routes
from routes.admin_routes import admin_routes
from routes.dev_routes import dev_routes
from routes.public_routes import public_routes
from state import app_state

from utils.lifecycle_utils import (
    record_program_shutdown,
    safe_close_trading_client,
)
from utils.logging_utils import configure_logging, handle_asyncio_exception

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

from state_init import ensure_app_state_structure

from app_config_init import (
    get_bool_env,
    load_environment_config,
)

from startup_tasks import background_startup_after_bind

load_dotenv()


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

        # Keep the legacy tick strategy observation-only by default.
        # Market-data collection and signal calculation remain active.
        app_state["execution"]["old_stream_strategy_enabled"] = (
            config.OLD_STREAM_STRATEGY_ENABLED
        )
        app_state["execution"]["layer4_execution_enabled"] = (
            config.LAYER4_EXECUTION_ENABLED
        )
        app_state["execution"]["layer3_market_hours_only"] = (
            config.LAYER3_MARKET_HOURS_ONLY
        )
        app_state["execution"]["layer3_bootstrap_confirmation_enabled"] = (
            config.LAYER3_BOOTSTRAP_CONFIRMATION_ENABLED
        )
        app_state["execution"]["layer3_bootstrap_min_bar_count"] = (
            config.LAYER3_BOOTSTRAP_MIN_BAR_COUNT
        )

        logging.info(
            "[Layer4Execution] execution_enabled=%s market_hours_only=%s "
            "bootstrap_confirmation_enabled=%s bootstrap_min_bar_count=%s",
            config.LAYER4_EXECUTION_ENABLED,
            config.LAYER3_MARKET_HOURS_ONLY,
            config.LAYER3_BOOTSTRAP_CONFIRMATION_ENABLED,
            config.LAYER3_BOOTSTRAP_MIN_BAR_COUNT,
        )

        if config.OLD_STREAM_STRATEGY_ENABLED:
            logging.warning(
                "[ExecutionMode] Legacy tick-by-tick stream execution is ENABLED."
            )
        else:
            logging.info(
                "[ExecutionMode] Legacy tick-by-tick stream execution is disabled; "
                "tick tracking remains active."
            )

        if config.LAYER4_EXECUTION_ENABLED:
            logging.warning(
                "[Layer3Execution] Layer 3 paper order execution is ENABLED."
            )
        else:
            logging.info(
                "[Layer3Execution] Layer 3 execution is disabled; dry-run planning only."
            )

        logging.info(f"ENABLE_DEV_ROUTES resolved to: {config.ENABLE_DEV_ROUTES}")

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
            background_startup_after_bind(),
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

# Evaluate this BEFORE router mounting so dev routes are included correctly.
config.ENABLE_DEV_ROUTES = get_bool_env("ENABLE_DEV_ROUTES", False)

app.include_router(public_routes, prefix="/api/public", tags=["public"])
app.include_router(auth_routes, prefix="/api/auth", tags=["auth"])
app.include_router(admin_routes, prefix="/api/admin", tags=["admin"])

if config.ENABLE_DEV_ROUTES:
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