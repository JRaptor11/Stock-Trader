# app_setup.py

import logging
import os

from fastapi.middleware.cors import CORSMiddleware

from routes.auth_routes import auth_routes
from routes.admin_routes import admin_routes
from routes.dev_routes import dev_routes
from routes.public_routes import public_routes

from core.config_loader import get_bool_env
from config import runtime_config as config


def configure_fastapi_app(app) -> None:
    """
    Configure middleware and route mounting for the FastAPI app.
    """

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

    logging.info("🔧 ENV is: %s", os.getenv("ENV", "development"))