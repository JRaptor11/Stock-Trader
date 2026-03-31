# public_routes.py

import time
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from state import app_state

public_routes = APIRouter()


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


@public_routes.get("/public-routes")
async def list_public_routes():
    routes = {
        "/": "Fast root uptime route",
        "/uptime-health": "Lightweight uptime monitor endpoint",
        "/public-routes": "List public routes",
    }
    return JSONResponse(routes)