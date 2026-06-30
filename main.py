# main.py
# This version is modified to use uvicorn instead of gunicorn + gevent
# Uvicorn allows direct async handling and works well for local or dev environments

import os
import time
import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from app_instance import app
from routes.auth_routes import auth_routes
from routes.admin_routes import admin_routes
from routes.dev_routes import dev_routes
from routes.public_routes import public_routes
from state import app_state

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

from shutdown_tasks import shutdown_trading_bot


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
        await shutdown_trading_bot()


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