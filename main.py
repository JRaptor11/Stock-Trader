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

from app_state_init import ensure_app_state_structure

from app_config_init import (
    get_bool_env,
    load_environment_config,
    apply_runtime_config_to_app_state,
    log_runtime_config_status,
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