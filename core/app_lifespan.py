# app_lifespan.py

import time
import logging
import asyncio
from contextlib import asynccontextmanager

from core.state import app_state

from utils.logging_utils import configure_logging, handle_asyncio_exception

from core.app_state_init import ensure_app_state_structure

from core.app_config_init import (
    load_environment_config,
    apply_runtime_config_to_app_state,
    log_runtime_config_status,
)

from core.startup_tasks import background_startup_after_bind

from core.shutdown_tasks import shutdown_trading_bot


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
        apply_runtime_config_to_app_state()
        log_runtime_config_status()

        if app_state["fail_safes"].get("position_lock") is None:
            app_state["fail_safes"]["position_lock"] = asyncio.Lock()

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