# admin_routes.py

import logging
import os
from datetime import datetime, timezone

import psutil
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from state import app_state
from stream import ThreadedAlpacaStream
from utils import config_utils as config
from utils.alerts_utils import send_email_alert
from utils.auth_utils import verify_credentials
from utils.config_utils import get_config, reset_config, set_config
from utils.misc_utils import with_retries

# ================================================================
# admin_routes.py
#
# Administrative control endpoints for the trading bot.
#
# These routes are used to control system components,
# inspect internal state, and update runtime configuration.
#
# Responsibilities
# ---------------------------------------------------------------
# • Stream lifecycle control
# • System monitoring and diagnostics
# • Telegram bot management
# • Runtime configuration updates
#
# ================================================================
#
# ROUTE TABLE
#
# ┌────────────────────┬────────┬──────────────────────────────────────┐
# │ Route              │ Method │ Description                          │
# ├────────────────────┼────────┼──────────────────────────────────────┤
# │ /start-stream      │ POST   │ Start Alpaca trade stream            │
# │ /shutdown-stream   │ POST   │ Stop Alpaca trade stream             │
# │ /stream-status     │ GET    │ Stream health and connection stats   │
# │ /execute           │ POST   │ Submit manual trade order            │
# ├────────────────────┼────────┼──────────────────────────────────────┤
# │ /metrics           │ GET    │ CPU and memory usage metrics         │
# │ /healthz           │ GET    │ Comprehensive service health check   │
# ├────────────────────┼────────┼──────────────────────────────────────┤
# │ /status-telegram   │ GET    │ Telegram bot status                  │
# │ /shutdown-telegram │ GET    │ Stop Telegram bot                    │
# ├────────────────────┼────────┼──────────────────────────────────────┤
# │ /force-alert       │ POST   │ Send manual alert                    │
# ├────────────────────┼────────┼──────────────────────────────────────┤
# │ /config            │ GET    │ Show config defaults & overrides     │
# │ /config/update     │ POST   │ Update runtime config value          │
# │ /config/reset      │ POST   │ Reset config override(s)             │
# ├────────────────────┼────────┼──────────────────────────────────────┤
# │ /admin-routes      │ GET    │ List admin routes                    │
# └────────────────────┴────────┴──────────────────────────────────────┘
#
# Notes
# ---------------------------------------------------------------
# • All routes require admin authentication
# • Used for operational control and monitoring
#
# ================================================================

admin_routes = APIRouter()

# ================================================================
# STREAM CONTROL ROUTES
# ================================================================

@admin_routes.post("/start-stream")
async def start_stream(credentials: str = Depends(verify_credentials)):
    """
    Manually start the trade data stream if it is not already running.
    """
    stream_instance = app_state["stream"].get("instance")

    if stream_instance and getattr(stream_instance, "_running", False):
        return {"status": "already_running", "message": "Stream is already running."}

    app_state["stream"]["instance"] = ThreadedAlpacaStream(
        config.API_KEY,
        config.SECRET_KEY,
        symbols=app_state["main"]["symbol"],
    )
    app_state["stream"]["instance"].start()

    return {"status": "started", "message": "Trade stream started."}


@admin_routes.post("/shutdown-stream")
async def shutdown_stream(credentials: str = Depends(verify_credentials)):
    """
    Manually stop the trade data stream if running.
    """
    try:
        stream_instance = app_state["stream"].get("instance")

        if stream_instance and getattr(stream_instance, "_running", False):
            logging.info("🛑 Manual shutdown requested via API.")
            stream_instance.stop()
            return {"message": "Stream shutdown requested."}

        return {"message": "Stream was not running or not initialized."}

    except Exception as e:
        logging.exception("Error during manual stream shutdown: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@admin_routes.get("/stream-status")
@with_retries()
async def stream_status(credentials: str = Depends(verify_credentials)):
    """
    Return detailed stream health and connection statistics.
    """
    stream_state = app_state.get("stream", {})
    manager = stream_state.get("manager")
    live_instance = stream_state.get("instance")
    connections = stream_state.get("connections", {})
    debug_snapshot = stream_state.get("debug", {})
    shutdown_event = stream_state.get("shutdown_event")
    thread = stream_state.get("thread")
    loop = stream_state.get("loop")

    state = stream_state.get("state", "unknown")
    running = stream_state.get("running", False)
    stopping = stream_state.get("stopping", False)

    status = {
        "status": state,
        "stream": {
            "state": state,
            "running": running,
            "stopping": stopping,
            "manager_exists": manager is not None,
            "manager_type": type(manager).__name__ if manager else None,
            "live_instance_exists": live_instance is not None,
            "live_instance_type": type(live_instance).__name__ if live_instance else None,
            "thread_exists": thread is not None,
            "thread_alive": bool(thread and thread.is_alive()),
            "loop_exists": loop is not None,
            "loop_running": bool(loop and loop.is_running()),
            "shutdown_event_set": bool(shutdown_event and shutdown_event.is_set()),
            "last_shutdown_duration": stream_state.get("last_shutdown_duration"),
        },
        "debug": debug_snapshot,
        "connections": {
            "total_attempts": connections.get("total_attempts", 0),
            "successful": connections.get("successful", 0),
            "failed": connections.get("failed", 0),
            "last_success": connections.get("last_success"),
            "last_failure": connections.get("last_failure"),
        },
    }

    if manager:
        status["internal"] = {
            "manager_running_flag": getattr(manager, "_running", False),
            "manager_thread_alive": bool(
                getattr(getattr(manager, "_thread", None), "is_alive", lambda: False)()
            ),
            "manager_loop_running": bool(
                getattr(getattr(manager, "_loop", None), "is_running", lambda: False)()
            ),
            "last_trade": getattr(manager, "_last_trade_handled", None),
        }

        last_trade = getattr(manager, "_last_trade_handled", None)
        if last_trade:
            last_trade_time = last_trade.get("timestamp")
            if last_trade_time:
                try:
                    dt = datetime.strptime(
                        last_trade_time, "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc)
                    seconds_since = (datetime.now(timezone.utc) - dt).total_seconds()
                    status["internal"]["seconds_since_last_trade"] = round(seconds_since, 2)
                except Exception:
                    pass

    return status

# ================================================================
# MANUAL EXECUTION ROUTES
# ================================================================

@admin_routes.post("/execute")
@with_retries()
async def execute_order(
    symbol: str,
    side: str,
    quantity: float,
    credentials: str = Depends(verify_credentials),
):
    """
    Submit a trade order through the order executor.
    """
    try:
        order_executor = app_state["services"]["order_executor"]["instance"]
        if order_executor is None:
            raise HTTPException(status_code=500, detail="Order executor is not initialized")

        await order_executor.submit_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=None,
        )

        return {"status": "order_submitted"}

    except HTTPException:
        raise
    except Exception as e:
        logging.error("Order execution failed: %s", str(e), exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))

# ================================================================
# SYSTEM MONITORING ROUTES
# ================================================================

@admin_routes.get("/metrics")
@with_retries()
async def metrics(credentials: str = Depends(verify_credentials)):
    """
    System resource usage report for CPU and memory.
    """
    process = psutil.Process(os.getpid())
    memory_mb = process.memory_info().rss / 1024 / 1024
    cpu_percent = process.cpu_percent(interval=0.5)

    return (
        f"CPU Usage: {cpu_percent:.2f}%\n"
        f"Memory Usage: {memory_mb:.2f} MB\n"
    )


@admin_routes.get("/healthz")
@admin_routes.head("/healthz", include_in_schema=False)
@with_retries()
async def health_check(credentials: str = Depends(verify_credentials)):
    """
    Comprehensive service-level health check.
    """
    services = {
        "data_stream": app_state["stream"].get("instance"),
        "balance_tracker": app_state["services"]["balance_tracker"]["instance"],
        "position_tracker": app_state["services"]["position_tracker"]["instance"],
    }

    status = {
        name: {
            "running": svc._running if svc and hasattr(svc, "_running") else bool(svc),
            "heartbeat": svc._heartbeat.is_alive() if svc and hasattr(svc, "_heartbeat") else None,
            "last_update": getattr(svc, "last_updated", None) if svc else None,
        }
        for name, svc in services.items()
    }

    summary_status = "OK" if all(v["running"] for v in status.values()) else "DEGRADED"
    return {"status": summary_status, "services": status}

# ================================================================
# TELEGRAM BOT CONTROL ROUTES
# ================================================================

@admin_routes.get("/status-telegram")
async def telegram_status(credentials: str = Depends(verify_credentials)):
    """
    Show Telegram bot status.
    """
    telegram = app_state.get("telegram", {})
    task = telegram.get("task")

    return {
        "enabled": telegram.get("enabled", False),
        "task_alive": (not task.done()) if task else False,
        "running": telegram.get("bot_app") is not None,
    }


@admin_routes.get("/shutdown-telegram")
async def shutdown_telegram(credentials: str = Depends(verify_credentials)):
    """
    Shut down the Telegram bot if it is running.
    """
    telegram = app_state.get("telegram", {})
    bot_app = telegram.get("bot_app")
    task = telegram.get("task")

    if not bot_app:
        return {"status": "not_running", "message": "Telegram bot is not running."}

    try:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()

        if task and not task.done():
            task.cancel()
            telegram["task"] = None

        telegram["enabled"] = False
        telegram["bot_app"] = None

        return {"status": "stopped", "message": "Telegram bot has been shut down."}

    except Exception as e:
        return {"status": "error", "message": str(e)}

# ================================================================
# ALERT ROUTES
# ================================================================

@admin_routes.post("/force-alert", response_class=PlainTextResponse)
@with_retries()
async def force_alert(credentials: str = Depends(verify_credentials)):
    """
    Manually trigger an alert.
    """
    send_email_alert("🚨 Manual Alert", "This is a manually triggered alert.")
    return "Alert sent."

# ================================================================
# RUNTIME CONFIGURATION ROUTES
# ================================================================

def _cast_config_value(key: str, value: str):
    """
    Cast incoming string values to the same type as the default config value.
    """
    defaults = app_state.get("config_defaults", {})

    if key not in defaults:
        raise ValueError(f"Unknown config key: {key}")

    default_value = defaults[key]

    # bool
    if isinstance(default_value, bool):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        raise ValueError(f"Invalid boolean value for {key}: {value}")

    # int
    if isinstance(default_value, int) and not isinstance(default_value, bool):
        return int(value)

    # float
    if isinstance(default_value, float):
        return float(value)

    # fallback: keep as string
    return value


@admin_routes.get("/config", dependencies=[Depends(verify_credentials)])
def get_all_config():
    """
    Show default, override, and effective config values.
    """
    defaults = app_state.get("config_defaults", {})
    overrides = app_state.get("config_overrides", {})

    return {
        "defaults": defaults,
        "overrides": overrides,
        "effective": {
            k: get_config(k)
            for k in defaults.keys()
        },
    }


@admin_routes.post("/config/update", dependencies=[Depends(verify_credentials)])
def update_config(
    key: str = Query(..., description="Config key to update"),
    value: str = Query(..., description="New value to set"),
):
    """
    Update a runtime config key using the correct type.
    """
    allowed_config_keys = {
        "TRADE_LIMIT",
        "TRADE_WINDOW",
        "TRADE_COOLDOWN",
        "BUY_ORDER_THROTTLE_SECONDS",
        "MIN_ORDER_AGE_SECONDS",
        "MIN_REENTRY_CHANGE_PCT",
        "BUY_CONFIDENCE_THRESHOLD",
        "SELL_CONFIDENCE_THRESHOLD",
        "EQUITY_THRESHOLD",
        "EQUITY_FAILSAFE_COOLDOWN",
        "MAX_POSITION_LOSS_PERCENT",
        "MAX_EQUITY_LOSS",
        "MAX_POSITION_LOSS",
        "MAX_CONNECTION_ERRORS",
        "CONNECTION_COOLDOWN",
        "MEMORY_ALERT_MB",
        "CPU_ALERT_PERCENT",
        "TRADE_RATE_RESPONSE",
    }

    if key not in allowed_config_keys:
        raise HTTPException(
            status_code=400,
            detail=f"Config key '{key}' is not allowed to be updated",
        )

    try:
        cast_value = _cast_config_value(key, value)
        set_config(key, cast_value)

        return {
            "message": f"{key} updated successfully",
            "effective": get_config(key),
            "type": type(get_config(key)).__name__,
            "persistence": "runtime_only",
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@admin_routes.post("/config/reset", dependencies=[Depends(verify_credentials)])
def reset_config_key(
    key: str | None = Query(None, description="Optional key to reset"),
):
    """
    Reset one config override or all overrides.
    """
    try:
        if key:
            reset_config(key)
            return {
                "message": f"{key} reset to default",
                "effective": get_config(key),
            }

        reset_config()
        return {"message": "All overrides cleared"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ================================================================
# ADMIN ROUTE DISCOVERY
# ================================================================

@admin_routes.get("/admin-routes", response_class=HTMLResponse)
async def admin_route_list(credentials: str = Depends(verify_credentials)):
    """
    Return a simple HTML list of admin routes.
    """
    route_descriptions = {
        "/start-stream": "Manually start the trade data stream",
        "/shutdown-stream": "Manually stop the trade data stream",
        "/stream-status": "Stream health and connection statistics",
        "/execute": "Submit a trade order",
        "/metrics": "System resource usage report",
        "/healthz": "Comprehensive service health check",
        "/status-telegram": "Telegram bot status",
        "/shutdown-telegram": "Shut down Telegram bot",
        "/force-alert": "Send manual alert",
        "/config": "Show default, override, and effective config values",
        "/config/update": "Update a runtime config key",
        "/config/reset": "Reset one or all runtime config overrides",
        "/admin-routes": "This admin route list",
    }

    html = "<html><body><h1>Admin Routes</h1><ul>"
    for path, desc in route_descriptions.items():
        html += f"<li><code>{path}</code> — {desc}</li>"
    html += "</ul></body></html>"

    return HTMLResponse(content=html)