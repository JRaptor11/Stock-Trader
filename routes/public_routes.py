# public_routes.py

import time
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from state import app_state

public_routes = APIRouter()

# ================================================================
# public_routes.py
#
# Public endpoints that require NO authentication.
#
# These routes are intentionally lightweight and safe for
# uptime monitors and public status checks.
#
# Responsibilities
# ---------------------------------------------------------------
# • External uptime monitoring
# • Basic system availability checks
# • Public route discovery
#
# ================================================================
#
# ROUTE TABLE
#
# ┌──────────────────┬────────┬──────────────────────────────────┐
# │ Route            │ Method │ Description                      │
# ├──────────────────┼────────┼──────────────────────────────────┤
# │ /uptime-health   │ GET    │ Lightweight uptime monitor       │
# │ /public-routes   │ GET    │ List public routes               │
# └──────────────────┴────────┴──────────────────────────────────┘
#
# Notes
# ---------------------------------------------------------------
# • No authentication required
# • Safe for uptime monitors (UptimeRobot, Pingdom, etc.)
# • Does NOT depend on Alpaca or trading services
#
# ================================================================

# ================================================================
# HEALTH / UPTIME
# ================================================================

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
# ROUTE DISCOVERY
# ================================================================

@public_routes.get("/public-routes")
async def list_public_routes():
    """
    List available public routes.
    Useful for quick verification in browsers or monitors.
    """

    routes = {
        "/uptime-health": "Lightweight uptime monitor endpoint",
        "/public-routes": "List public routes",
    }

    return JSONResponse(routes)