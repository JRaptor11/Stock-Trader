# routes/admin_routes.py

import logging
import os
from datetime import datetime, timezone

import psutil
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from core.state import app_state
from market.stream import ThreadedAlpacaStream
from config import runtime_config as config
from integrations.alerts import send_email_alert
from integrations.auth import verify_credentials
from config.runtime_config import get_config, reset_config, set_config
from utils.misc_utils import with_retries


# ================================================================
#
# Administrative control and operational monitoring endpoints for
# the trading bot.
#
# These routes are used to control system components, inspect live
# runtime health, manage Telegram/background services, view Layer
# system status, and update runtime configuration.
#
# These routes should NOT be exposed publicly unless properly secured.
#
# ---------------------------------------------------------------
# Responsibilities
# ---------------------------------------------------------------
# • Stream lifecycle control
# • Manual order execution
# • System health and resource monitoring
# • Layer 1/2/3/4 operational status inspection
# • Telegram bot management
# • Manual alert triggering
# • Runtime configuration inspection and updates
#
# ---------------------------------------------------------------
# ROUTE TABLE
# ---------------------------------------------------------------
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
# │ /layer-status      │ GET    │ Current Layer system status          │
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
# • Intended for operational control and monitoring
# • Some routes are read-only; others can start/stop services,
#   submit manual orders, send alerts, or update runtime config
# • /layer-status is read-only and does not execute trades
#
# ================================================================


admin_routes = APIRouter()


# ================================================================
# STREAM CONTROL ROUTES
# ---------------------------------------------------------------
# Routes for starting, stopping, and inspecting the Alpaca trade
# stream and its background thread/loop state.
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
# ---------------------------------------------------------------
# Routes that can submit manual orders through the execution path.
# Use carefully while the bot is live.
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
# ---------------------------------------------------------------
# Read-only operational health routes for process resources,
# service health, and current Layer 1/2/3/4 runtime status.
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


@admin_routes.get("/layer-status")
@with_retries()
async def layer_status(credentials: str = Depends(verify_credentials)):
    """
    Return current in-memory Layer 1/2/3/4 operational status.

    This route does not read CSVs and does not place trades.
    It is meant for quick operational checks after startup/deploys.
    """
    now = datetime.now(timezone.utc).isoformat()

    layers = app_state.get("layers", {})
    execution = app_state.get("execution", {})
    main = app_state.get("main", {})

    latest = layers.get("latest", {})
    rebalance = layers.get("rebalance", {})
    layer4 = layers.get("layer4", {})
    layer4_execution = layers.get("layer4_execution", {})
    active_plan = layers.get("active_execution_plan")
    bar_freshness = layers.get("bar_freshness", {})

    if not isinstance(latest, dict):
        latest = {}

    if not isinstance(rebalance, dict):
        rebalance = {}

    if not isinstance(layer4, dict):
        layer4 = {}

    if not isinstance(layer4_execution, dict):
        layer4_execution = {}

    if not isinstance(bar_freshness, dict):
        bar_freshness = {}

    configured_symbols = list(main.get("symbol", []) or [])

    ranked = latest.get("ranked", [])
    if not isinstance(ranked, list):
        ranked = []

    target_portfolio = latest.get("target_portfolio", {})
    if not isinstance(target_portfolio, dict):
        target_portfolio = {}

    target_meta = latest.get("target_meta", {})
    if not isinstance(target_meta, dict):
        target_meta = {}

    target_symbols = [
        symbol
        for symbol in target_portfolio.keys()
        if str(symbol).upper().strip() not in {"CASH", "_META"}
        and not str(symbol).startswith("_")
    ]

    target_summary = {
        "target_symbol_count": len(target_symbols),
        "target_symbols": target_symbols,
        "cash_pct": target_portfolio.get("CASH"),
        "market_strength": target_meta.get("market_strength"),
        "weighting_mode": target_meta.get("weighting_mode"),
        "top_score": target_meta.get("top_score"),
        "avg_top_score": target_meta.get("avg_top_score"),
    }

    last_plan = rebalance.get("last_plan", [])
    if not isinstance(last_plan, list):
        last_plan = []

    last_plan_decision_counts = {}
    for row in last_plan:
        if not isinstance(row, dict):
            continue

        decision = str(row.get("decision") or "unknown")
        last_plan_decision_counts[decision] = last_plan_decision_counts.get(decision, 0) + 1

    active_plan_summary = None

    if isinstance(active_plan, dict):
        active_plan_rows = active_plan.get("rows", [])
        if not isinstance(active_plan_rows, list):
            active_plan_rows = []

        active_plan_decision_counts = {}
        for row in active_plan_rows:
            if not isinstance(row, dict):
                continue

            decision = str(row.get("decision") or "unknown")
            active_plan_decision_counts[decision] = active_plan_decision_counts.get(decision, 0) + 1

        active_plan_summary = {
            "plan_id": active_plan.get("plan_id"),
            "status": active_plan.get("status"),
            "created_at": active_plan.get("created_at"),
            "expires_at": active_plan.get("expires_at"),
            "ttl_seconds": active_plan.get("ttl_seconds"),
            "row_count": len(active_plan_rows),
            "decision_counts": active_plan_decision_counts,
            "summary": active_plan.get("summary"),
        }

    last_layer4_result = layer4_execution.get("last_result")
    compact_layer4_result = None

    if isinstance(last_layer4_result, dict):
        orders = last_layer4_result.get("orders", [])
        if not isinstance(orders, list):
            orders = []

        compact_layer4_result = {
            "cycle_id": last_layer4_result.get("cycle_id"),
            "plan_id": last_layer4_result.get("plan_id"),
            "enabled": last_layer4_result.get("enabled"),
            "started_at": last_layer4_result.get("started_at"),
            "finished_at": last_layer4_result.get("finished_at"),
            "duration_seconds": last_layer4_result.get("duration_seconds"),
            "attempted": last_layer4_result.get("attempted"),
            "submitted": last_layer4_result.get("submitted"),
            "skipped": last_layer4_result.get("skipped"),
            "errors": last_layer4_result.get("errors"),
            "blocked_reason": last_layer4_result.get("blocked_reason"),
            "count_integrity_ok": last_layer4_result.get("count_integrity_ok"),
            "order_count": len(orders),
        }

    tick_counts = {}
    market_data_buffer = app_state.get("market_data", {}).get("buffer")

    if market_data_buffer is not None:
        for symbol in configured_symbols:
            try:
                tick_counts[symbol] = len(market_data_buffer.get_recent_prices(symbol))
            except Exception:
                tick_counts[symbol] = None

    issues = []

    if layers.get("engine") is None:
        issues.append("layer_engine_missing")

    if layers.get("paper_portfolio") is None:
        issues.append("paper_portfolio_missing")

    if not latest:
        issues.append("no_layer12_snapshot")

    if rebalance.get("last_error"):
        issues.append("layer3_last_error")

    if compact_layer4_result and compact_layer4_result.get("count_integrity_ok") is False:
        issues.append("layer4_count_integrity_failed")

    status = "ok" if not issues else "degraded"

    return {
        "status": status,
        "issues": issues,
        "timestamp": now,
        "configured_symbols": configured_symbols,
        "execution": {
            "old_stream_strategy_enabled": execution.get("old_stream_strategy_enabled"),
            "layer3_execution_enabled": execution.get("layer3_execution_enabled"),
            "layer4_execution_enabled": execution.get("layer4_execution_enabled"),
            "layer3_market_hours_only": execution.get("layer3_market_hours_only"),
            "layer3_bootstrap_confirmation_enabled": execution.get("layer3_bootstrap_confirmation_enabled"),
            "layer3_bootstrap_min_bar_count": execution.get("layer3_bootstrap_min_bar_count"),
        },
        "initialization": {
            "layer_engine_exists": layers.get("engine") is not None,
            "layer_engine_type": type(layers.get("engine")).__name__ if layers.get("engine") else None,
            "paper_portfolio_exists": layers.get("paper_portfolio") is not None,
            "paper_portfolio_type": type(layers.get("paper_portfolio")).__name__ if layers.get("paper_portfolio") else None,
        },
        "market_data": {
            "tick_counts": tick_counts,
        },
        "latest_layer12": {
            "timestamp": latest.get("timestamp"),
            "symbols_evaluated": latest.get("symbols_evaluated"),
            "bar_counts": latest.get("bar_counts"),
            "ranked_count": len(ranked),
            "top_ranked": ranked[:5],
            "target_summary": target_summary,
        },
        "bar_freshness": bar_freshness,
        "layer3": {
            "enabled": rebalance.get("enabled"),
            "dry_run": rebalance.get("dry_run"),
            "last_cycle_id": rebalance.get("last_cycle_id"),
            "last_run_at": rebalance.get("last_run_at"),
            "last_error": rebalance.get("last_error"),
            "last_plan_count": len(last_plan),
            "last_plan_decision_counts": last_plan_decision_counts,
            "last_summary": rebalance.get("last_summary"),
            "target_seen_counts": rebalance.get("target_seen_counts"),
            "target_absent_counts": rebalance.get("target_absent_counts"),
            "bootstrap_confirmation_applied": rebalance.get("bootstrap_confirmation_applied"),
            "bootstrap_confirmation_symbols": rebalance.get("bootstrap_confirmation_symbols"),
            "confirmation_updates_allowed": rebalance.get("confirmation_updates_allowed"),
            "confirmation_updates_blocked_reason": rebalance.get("confirmation_updates_blocked_reason"),
        },
        "active_execution_plan": active_plan_summary,
        "layer4": {
            "active_plan_id": layer4.get("active_plan_id"),
            "active_plan_expires_at": layer4.get("active_plan_expires_at"),
            "last_attempted_at": layer4_execution.get("last_attempted_at"),
            "last_cycle_id": layer4_execution.get("last_cycle_id"),
            "last_plan_id": layer4_execution.get("last_plan_id"),
            "last_result": compact_layer4_result,
        },
    }


# ================================================================
# TELEGRAM BOT CONTROL ROUTES
# ---------------------------------------------------------------
# Routes for inspecting and stopping the Telegram bot integration.
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
# ---------------------------------------------------------------
# Routes that manually trigger alert delivery for operational testing.
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
# ---------------------------------------------------------------
# Routes for inspecting defaults, applying runtime-only config
# overrides, and resetting config overrides.
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
        "CONFIDENCE_CONFLICT_MARGIN",
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
# ---------------------------------------------------------------
# Read-only route index for available authenticated admin endpoints.
# ================================================================

@admin_routes.get("/admin-routes", response_class=HTMLResponse)
async def admin_route_list(credentials: str = Depends(verify_credentials)):
    """
    Return an HTML list of authenticated admin routes.
    """
    route_descriptions = {
        "/start-stream": "Manually start the trade data stream",
        "/shutdown-stream": "Manually stop the trade data stream",
        "/stream-status": "Stream health and connection statistics",
        "/execute": "Submit a trade order",
        "/metrics": "System resource usage report",
        "/healthz": "Comprehensive service health check",
        "/layer-status": "Current in-memory Layer 1/2/3/4 operational status",
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