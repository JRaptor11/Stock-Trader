# routes/public_routes.py

import time
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.state import app_state


# ================================================================
#
# Public unauthenticated endpoints for basic service availability.
#
# These routes are intentionally lightweight and should not expose
# trading state, positions, symbols, Layer diagnostics, credentials,
# config values, or other internal runtime details.
#
# ---------------------------------------------------------------
# Responsibilities
# ---------------------------------------------------------------
# • Fast root health response for Render / uptime checks
# • Lightweight uptime monitor endpoint
# • Public route discovery
#
# ---------------------------------------------------------------
# ROUTE TABLE
# ---------------------------------------------------------------
#
# ┌────────────────┬──────────┬────────────────────────────────────┐
# │ Route          │ Method   │ Description                        │
# ├────────────────┼──────────┼────────────────────────────────────┤
# │ /              │ GET/HEAD │ Fast root uptime route             │
# │ /uptime-health │ GET/HEAD │ Lightweight uptime monitor route   │
# │ /public-routes │ GET      │ List public routes                 │
# └────────────────┴──────────┴────────────────────────────────────┘
#
# Notes
# ---------------------------------------------------------------
# • No authentication required
# • Keep responses fast and dependency-light
# • Do not expose trading internals from this router
#
# ================================================================


public_routes = APIRouter()


# ================================================================
# PUBLIC HEALTH ROUTES
# ---------------------------------------------------------------
# Lightweight health routes used by Render, uptime monitors, or
# basic service availability checks.
# ================================================================

@public_routes.api_route("/", methods=["GET", "HEAD"])
async def root_health():
    """
    Fast root route for Render/UptimeRobot.
    """
    try:
        return JSONResponse(
            {
                "status": "ok",
                "service": "stock-trader-bot",
                "uptime": round(time.time() - app_state.get("startup_time", time.time()), 2),
            }
        )
    except Exception as e:
        logging.exception("[PublicRoutes] root health failure")
        return JSONResponse(
            {
                "status": "error",
                "error": str(e),
            },
            status_code=500,
        )


@public_routes.api_route("/uptime-health", methods=["GET", "HEAD"])
async def uptime_health_check():
    """
    Lightweight health check used by uptime monitors.
    Does NOT depend on trading client or external APIs.
    """
    try:
        return JSONResponse(
            {
                "status": "ok",
                "uptime": round(time.time() - app_state.get("startup_time", time.time()), 2),
            }
        )
    except Exception as e:
        logging.exception("[PublicRoutes] uptime-health failure")
        return JSONResponse(
            {
                "status": "error",
                "error": str(e),
            },
            status_code=500,
        )


# ================================================================
# PUBLIC ROUTE DISCOVERY
# ---------------------------------------------------------------
# Read-only route index for public unauthenticated endpoints.
# ================================================================

@public_routes.get("/public-routes")
async def list_public_routes():
    """
    Return a list of public unauthenticated routes.
    """
    routes = {
        "/": "Fast root uptime route",
        "/uptime-health": "Lightweight uptime monitor endpoint",
        "/public-routes": "List public routes",
    }
    return JSONResponse(routes)