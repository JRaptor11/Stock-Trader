# routes/dev_routes.py

import os
import csv
import io
import json
import tempfile
import zipfile
from collections import Counter, defaultdict

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse

from core.state import app_state
from config import runtime_config as config
from integrations.alerts import send_email_alert, send_telegram_alert
from trading.trade_utils import TRADE_REASON_LOG
from integrations.auth import verify_credentials
from utils.numeric import safe_float, safe_int
from utils.symbols import normalize_symbol
from utils.misc_utils import with_retries
from market.stream import FakeTrade

from layers.layer_csv import LAYER_CSV_FILES, layer_csv_path, read_csv_rows

# ================================================================
#
# Development and debugging endpoints used for testing trading logic,
# inspecting runtime state, reviewing CSV diagnostics, and manually
# triggering controlled debug actions.
#
# These routes should NOT be exposed in production environments unless
# properly secured.
#
# ---------------------------------------------------------------
# Responsibilities
# ---------------------------------------------------------------
# • Trade simulation and manual debug actions
# • Runtime diagnostics and state inspection
# • Legacy tick-strategy CSV inspection
# • Layer 3 / Layer 4 CSV diagnostics
# • Manual alert testing
#
# ---------------------------------------------------------------
# ROUTE GROUPS
# ---------------------------------------------------------------
#
# 1. Route Discovery
#    Read-only index route for finding available dev/debug endpoints.
#
# 2. Read-Only Runtime State
#    Read-only routes for current positions, open trades, strategy state,
#    in-memory trade snapshots, and system diagnostics.
#
# 3. Legacy CSV Routes
#    Read-only CSV routes from the older tick-by-tick strategy path.
#    These are still useful for legacy signal analysis and rough
#    buy/sell summaries, but they are not the source of truth for
#    Layer 3 / Layer 4 portfolio rebalancing.
#
# 4. Layer CSV Diagnostics
#    Read-only CSV and derived analysis routes for the current
#    Layer 3 / Layer 4 portfolio planning and execution system.
#    These should be the preferred audit trail for cycle health,
#    rebalance plans, Layer 4 orders, skipped cycles, allocation drift,
#    and operational effectiveness.
#
# 5. Action Routes — Manual Execution
#    Routes that can force debug buy/sell behavior through the execution
#    path. Use carefully while the bot is live.
#
# 6. Action Routes — Trade Simulation
#    Routes that inject fake trade ticks or simulated test sequences into
#    the stream handler. Use carefully during live market validation.
#
# 7. Action Routes — Alerts / Balance Testing
#    Routes that send test alerts or force one balance update.
#
# Notes
# ---------------------------------------------------------------
# • Router protected by authentication
# • Intended for development/testing only
# • Some routes are read-only; others may trigger trades, fake ticks, or alerts
# • Layer CSV routes are the preferred diagnostics for Layer 3 / Layer 4
#
# ================================================================

dev_routes = APIRouter(dependencies=[Depends(verify_credentials)])


def get_stream_manager():
    manager = app_state.get("stream", {}).get("manager")
    if not manager:
        raise HTTPException(status_code=503, detail="Stream manager is not available")
    return manager


def _filter_rows_by_symbol(rows: list[dict], symbol: str | None, symbol_keys: list[str]) -> list[dict]:
    """
    Filter CSV DictReader rows by symbol using one of several possible column names.
    """
    symbol = normalize_symbol(symbol)
    if not symbol:
        return rows

    filtered = []
    for row in rows:
        for key in symbol_keys:
            value = row.get(key)
            if normalize_symbol(value) == symbol:
                filtered.append(row)
                break

    return filtered


def _read_csv_rows(path: str) -> list[dict]:
    rows = []
    with open(path, mode="r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def _rows_to_csv_text(rows: list[dict], fieldnames: list[str] | None = None) -> str:
    output = io.StringIO()

    if fieldnames is None:
        if rows:
            fieldnames = list(rows[0].keys())
        else:
            return ""

    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    if rows:
        writer.writerows(rows)
    return output.getvalue()


def _layer_csv_filename(name: str) -> str:
    filename = LAYER_CSV_FILES.get(name)

    if not filename:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown layer CSV '{name}'. Available: {sorted(LAYER_CSV_FILES)}",
        )

    return filename


def _safe_bool_value(value) -> bool:
    if isinstance(value, bool):
        return value

    return str(value or "").strip().lower() in {"true", "1", "yes", "on"}


def _json_value(value, default=None):
    if value in (None, ""):
        return default

    if isinstance(value, (dict, list)):
        return value

    try:
        return json.loads(value)
    except Exception:
        return default


def _counter_to_dict(counter: Counter, limit: int | None = None) -> dict:
    items = counter.most_common(limit) if limit else counter.most_common()
    return {key: count for key, count in items}


def _layer_csv_data(name: str, limit: int = 10000) -> dict:
    filename = _layer_csv_filename(name)
    limit = max(1, min(safe_int(limit, 10000), 10000))
    return read_csv_rows(filename, limit=limit)


def _layer_csv_rows(name: str, limit: int = 10000) -> list[dict]:
    data = _layer_csv_data(name, limit=limit)
    rows = data.get("rows", [])
    return rows if isinstance(rows, list) else []


def _row_cycle_id(row: dict) -> str:
    return str(row.get("cycle_id") or "").strip()


def _row_symbol(row: dict) -> str:
    return normalize_symbol(row.get("symbol"))


# ================================================================
# ROUTE DISCOVERY
# ---------------------------------------------------------------
# Read-only route index pages for finding available dev/debug routes.
# ================================================================

@dev_routes.get("/dev-routes", response_class=HTMLResponse)
def dev_route_list():
    """
    Return a grouped HTML list of dev/debug routes.

    Route groups intentionally separate:
    - read-only inspection routes
    - legacy tick-strategy CSV routes
    - current Layer 3 / Layer 4 CSV diagnostics
    - action routes that may trigger execution, fake ticks, alerts, or balance updates
    """
    route_groups = [
        (
            "Route Discovery",
            "Read-only route index pages for finding available dev/debug endpoints.",
            {
                "/dev-routes": "GET — Show this grouped dev/debug route list",
            },
        ),
        (
            "Read-Only Runtime State",
            "Read-only routes for current broker positions, open trades, strategy state, in-memory snapshots, and diagnostics.",
            {
                "/test-positions": "GET — Show current Alpaca positions",
                "/debug/open-trades": "GET — Show raw open_trades state",
                "/strategy-state": "GET — Show current in-memory strategy state",
                "/trades-live": "GET — View current in-memory open trades and latest signal state",
                "/trades-csv-live": "GET — Download a simple CSV snapshot of current in-memory open trades",
                "/diagnostics": "GET — Run a system diagnostics snapshot",
            },
        ),
        (
            "Legacy CSV Routes — Old Tick-Strategy / Round-Trip Logs",
            "Older CSVs from the tick-by-tick strategy path. Useful for legacy signal analysis and rough P/L review, but not the preferred Layer 3 / Layer 4 audit trail.",
            {
                "/trade-history": "GET — View parsed trade_history.csv rows as JSON",
                "/trade-history-csv": "GET — Download trade_history.csv as CSV text",
                "/trade-summary": "GET — View parsed trade_summary.csv rows as JSON. Legacy buy→sell pairing summary.",
                "/trade-summary-csv": "GET — Download trade_summary.csv as CSV text",
                "/trade-decisions": "GET — View parsed trade_decisions_log.csv rows as JSON. Legacy tick-level strategy decision log.",
                "/trade-decisions-csv": "GET — Download trade_decisions_log.csv as CSV text",
            },
        ),
        (
            "Layer CSV Diagnostics — Current Layer 3 / Layer 4 Audit Trail",
            "Preferred diagnostics for the layered portfolio system: cycle health, Layer 3 plans, Layer 4 orders, skipped cycles, and allocation snapshots.",
            {
                "/layers/layer-routes": "GET — Show only Layer CSV route list",
                "/layers/cycles": "GET — View parsed layer_cycles.csv rows as JSON",
                "/layers/cycles-csv": "GET — Download layer_cycles.csv",
                "/layers/plans": "GET — View parsed layer3_plans.csv rows as JSON",
                "/layers/plans-csv": "GET — Download layer3_plans.csv",
                "/layers/orders": "GET — View parsed layer4_orders.csv rows as JSON",
                "/layers/orders-csv": "GET — Download layer4_orders.csv",
                "/layers/portfolio-snapshots": "GET — View parsed layer_portfolio_snapshots.csv rows as JSON",
                "/layers/portfolio-snapshots-csv": "GET — Download layer_portfolio_snapshots.csv",
                "/layers/shadow": "GET — View parsed layer4_shadow.csv rows as JSON",
                "/layers/shadow-csv": "GET — Download layer4_shadow.csv",
                "/layers/live-strategy-shadow": "View parsed layer_live_strategy_shadow.csv rows as JSON",
                "/layers/live-strategy-shadow-csv": "Download layer_live_strategy_shadow.csv",
                "/layers/live-strategy-shadow-cycles": "View parsed layer_live_strategy_shadow_cycles.csv rows as JSON",
                "/layers/live-strategy-shadow-cycles-csv": "Download layer_live_strategy_shadow_cycles.csv",
                "/layers/live-strategy-outcomes": "View parsed layer_live_strategy_outcomes.csv rows as JSON",
                "/layers/live-strategy-outcomes-csv": "Download layer_live_strategy_outcomes.csv",
                "/layers/all-csv-diagnostics.zip": "Download all available Layer CSV diagnostics as one ZIP",
                "/layers/dashboard": "GET — Summarize latest Layer cycle health, skips, plan state, and order state",
                "/layers/cycle/{cycle_id}": "GET — Join cycle, plan, order, and snapshot rows for one Layer cycle",
                "/layers/symbol/{symbol}": "GET — Summarize Layer 3 / Layer 4 behavior for one symbol",
                "/layers/effectiveness": "GET — Summarize operational Layer effectiveness, skip reasons, execution rates, and symbol activity",
            },
        ),
        (
            "Action Routes — Manual Execution",
            "Routes in this section can force debug execution behavior. Use carefully while the bot is live.",
            {
                "/debug-buy": "POST — Force a debug buy through the execution path",
                "/debug-sell": "POST — Force a debug sell through the execution path",
            },
        ),
        (
            "Action Routes — Trade Simulation",
            "Routes in this section inject fake trade ticks or run simulated test sequences through the stream handler.",
            {
                "/simulate-buy": "POST — Inject a fake trade tick into the normal trade handler",
                "/simulate-sell": "POST — Inject a fake trade tick into the normal trade handler",
                "/simulate-100-trades": "POST — Run 100 fake trade ticks for stress testing",
                "/test-sequence": "POST — Run a simple fake buy → sell tick sequence",
                "/test-buy-only": "POST — Inject a single fake trade tick for buy-side testing",
                "/test-sell-only": "POST — Inject a single fake trade tick for sell-side testing",
            },
        ),
        (
            "Action Routes — Alerts / Balance Testing",
            "Routes in this section can send test alerts or force one account balance refresh.",
            {
                "/test-alert": "POST — Send a test email alert",
                "/test-telegram": "POST — Send a test Telegram alert",
                "/test-update-balance": "POST — Force one account balance update",
            },
        ),
    ]

    html = "<html><body><h1>Dev Routes</h1>"

    html += (
        "<p><strong>Note:</strong> Read-only routes are listed first. "
        "Action routes can trigger execution paths, fake ticks, alerts, or balance updates. "
        "Legacy CSV routes are from the older tick-by-tick strategy path. "
        "Layer CSV routes are the preferred diagnostics for the current Layer 3 / Layer 4 system.</p>"
    )

    for group_name, group_description, routes in route_groups:
        html += f"<h2>{group_name}</h2>"
        html += f"<p>{group_description}</p>"
        html += "<ul>"

        for path, desc in routes.items():
            html += f"<li><code>{path}</code> — {desc}</li>"

        html += "</ul>"

    html += "</body></html>"

    return HTMLResponse(content=html)


# ================================================================
# READ-ONLY RUNTIME STATE
# ---------------------------------------------------------------
# Read-only runtime inspection routes for broker positions, open trades,
# strategy state, in-memory snapshots, and diagnostics.
# ================================================================

@dev_routes.get("/test-positions")
@with_retries()
def test_positions():
    """
    Show current tracked positions from the trading client.
    """
    try:
        trading_client = app_state.get("trading_client")
        if trading_client is None:
            raise HTTPException(status_code=500, detail="Trading client is not initialized")

        positions = trading_client.get_all_positions()
        return {
            "count": len(positions),
            "positions": [str(p) for p in positions],
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.exception("[DevRoutes] test-positions failed")
        raise HTTPException(status_code=500, detail=str(e))


@dev_routes.get("/debug/open-trades")
@with_retries()
def debug_open_trades():
    """
    View the raw open_trades structure.
    """
    return {
        "open_trades": app_state.get("open_trades", {}),
        "count": len(app_state.get("open_trades", {})),
    }


@dev_routes.get("/strategy-state")
@with_retries()
def view_strategy_state():
    """
    View recent strategy / decision state snapshot.
    """
    strategy = app_state.get("strategy", {})
    return {
        "latest_rsi": strategy.get("latest_rsi"),
        "last_exit_reason": strategy.get("last_exit_reason"),
        "last_sell": strategy.get("last_sell"),
        "cooldown_until": strategy.get("cooldown_until"),
        "consecutive_losses": strategy.get("consecutive_losses"),
        "last_strategy_results": strategy.get("last_strategy_results"),
        "last_buy_confidence": strategy.get("last_buy_confidence"),
        "last_sell_confidence": strategy.get("last_sell_confidence"),
    }


@dev_routes.get("/trades-live")
@with_retries()
def view_trade_log():
    """
    View completed trades from in-memory open_trades / trade history structures.
    """
    return {
        "open_trades": app_state.get("open_trades", {}),
        "last_trade_time": app_state.get("last_trade_time"),
        "last_signal": app_state.get("last_signal"),
    }


@dev_routes.get("/trades-csv-live", response_class=PlainTextResponse)
@with_retries()
def view_trade_log_csv():
    """
    Return a simple CSV-style snapshot of open trades.
    """
    lines = ["symbol,status,data"]

    for symbol, data in app_state.get("open_trades", {}).items():
        lines.append(f"{symbol},open,\"{data}\"")

    return "\n".join(lines)


@dev_routes.get("/diagnostics")
@with_retries()
def run_diagnostics():
    """
    Return a basic diagnostics snapshot.
    """
    telegram = app_state.get("telegram", {})
    stream = app_state.get("stream", {})

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stream_running": stream.get("state") == "running",
        "stream_state": stream.get("state"),
        "telegram_enabled": telegram.get("enabled", False),
        "telegram_started": telegram.get("bot_started", False),
        "open_trades_count": len(app_state.get("open_trades", {})),
        "last_signal": app_state.get("last_signal"),
        "last_trade_time": app_state.get("last_trade_time"),
        "connection_error_count": app_state.get("connection_error_count", 0),
        "dev_routes_enabled": getattr(config, "ENABLE_DEV_ROUTES", False),
    }


# ================================================================
# LEGACY CSV ROUTES
# ---------------------------------------------------------------
# Read-only CSV routes from the older tick-by-tick strategy path.
# Useful for legacy signal analysis and rough buy/sell summaries,
# but not the source of truth for Layer 3 / Layer 4.
# ================================================================


@dev_routes.get("/trade-history")
@with_retries()
def view_trade_history(symbol: str | None = Query(default=None)):
    """
    Return parsed rows from trade_history.csv as JSON.
    Optional ?symbol=NVDA filter.
    """
    path = app_state.get("paths", {}).get("TRADE_HISTORY_FILE", "trade_history.csv")

    if not os.path.exists(path):
        return {
            "status": "missing",
            "file": path,
            "symbol_filter": normalize_symbol(symbol),
            "rows": [],
            "count": 0,
            "message": "trade_history.csv not found",
        }

    rows = _read_csv_rows(path)
    rows = _filter_rows_by_symbol(rows, symbol, ["Symbol", "symbol"])

    return {
        "status": "ok",
        "file": path,
        "symbol_filter": normalize_symbol(symbol),
        "count": len(rows),
        "rows": rows,
    }


@dev_routes.get("/trade-history-csv", response_class=PlainTextResponse)
@with_retries()
def download_trade_history_csv(symbol: str | None = Query(default=None)):
    """
    Return raw trade_history.csv contents.
    Optional ?symbol=NVDA filter.
    """
    path = app_state.get("paths", {}).get("TRADE_HISTORY_FILE", "trade_history.csv")

    if not os.path.exists(path):
        return f"file_not_found,{path}"

    all_rows = _read_csv_rows(path)
    filtered_rows = _filter_rows_by_symbol(all_rows, symbol, ["Symbol", "symbol"])

    fieldnames = list(all_rows[0].keys()) if all_rows else None
    return _rows_to_csv_text(filtered_rows, fieldnames=fieldnames)


@dev_routes.get("/trade-summary")
@with_retries()
def view_trade_summary(symbol: str | None = Query(default=None)):
    """
    Return parsed rows from trade_summary.csv as JSON.
    Optional ?symbol=NVDA filter.
    """
    path = app_state.get("paths", {}).get("TRADE_SUMMARY_FILE", "trade_summary.csv")

    if not os.path.exists(path):
        return {
            "status": "missing",
            "file": path,
            "symbol_filter": normalize_symbol(symbol),
            "rows": [],
            "count": 0,
            "message": "trade_summary.csv not found",
        }

    rows = _read_csv_rows(path)
    rows = _filter_rows_by_symbol(rows, symbol, ["Symbol", "symbol"])

    return {
        "status": "ok",
        "file": path,
        "symbol_filter": normalize_symbol(symbol),
        "count": len(rows),
        "rows": rows,
    }


@dev_routes.get("/trade-summary-csv", response_class=PlainTextResponse)
@with_retries()
def download_trade_summary_csv(symbol: str | None = Query(default=None)):
    """
    Return raw trade_summary.csv contents.
    Optional ?symbol=NVDA filter.
    """
    path = app_state.get("paths", {}).get("TRADE_SUMMARY_FILE", "trade_summary.csv")

    if not os.path.exists(path):
        return f"file_not_found,{path}"

    all_rows = _read_csv_rows(path)
    filtered_rows = _filter_rows_by_symbol(all_rows, symbol, ["Symbol", "symbol"])

    fieldnames = list(all_rows[0].keys()) if all_rows else None
    return _rows_to_csv_text(filtered_rows, fieldnames=fieldnames)


@dev_routes.get("/trade-decisions")
@with_retries()
def view_trade_decisions(symbol: str | None = Query(default=None)):
    """
    Return parsed rows from trade_decisions_log.csv as JSON.
    Optional ?symbol=NVDA filter.
    """
    path = TRADE_REASON_LOG

    if not os.path.exists(path):
        return {
            "status": "missing",
            "file": path,
            "symbol_filter": normalize_symbol(symbol),
            "rows": [],
            "count": 0,
            "message": "trade_decisions_log.csv not found",
        }

    rows = _read_csv_rows(path)
    rows = _filter_rows_by_symbol(rows, symbol, ["symbol", "Symbol"])

    return {
        "status": "ok",
        "file": path,
        "symbol_filter": normalize_symbol(symbol),
        "count": len(rows),
        "rows": rows,
    }


@dev_routes.get("/trade-decisions-csv", response_class=PlainTextResponse)
@with_retries()
def download_trade_decisions_csv(symbol: str | None = Query(default=None)):
    """
    Return raw trade_decisions_log.csv contents.
    Optional ?symbol=NVDA filter.
    """
    path = TRADE_REASON_LOG

    if not os.path.exists(path):
        return f"file_not_found,{path}"

    all_rows = _read_csv_rows(path)
    filtered_rows = _filter_rows_by_symbol(all_rows, symbol, ["symbol", "Symbol"])

    fieldnames = list(all_rows[0].keys()) if all_rows else None
    return _rows_to_csv_text(filtered_rows, fieldnames=fieldnames)


# ================================================================
# LAYER CSV DIAGNOSTICS
# ---------------------------------------------------------------
# Read-only CSV routes and derived analysis routes for the current
# Layer 3 / Layer 4 portfolio planning, execution, skipped-cycle,
# and allocation-drift audit trail.
# ================================================================


@dev_routes.get("/layers/all-csv-diagnostics.zip")
@with_retries()
def download_all_layer_csv_diagnostics():
    """
    Download every available Layer CSV diagnostic file in a single ZIP.

    This includes the standard Layer 3/4/5 audit trail plus live-bar shadow
    comparison CSVs when they exist.
    """
    available_files = []

    for logical_name, filename in sorted(LAYER_CSV_FILES.items()):
        path = layer_csv_path(filename)
        if path.exists() and path.is_file():
            available_files.append((logical_name, filename, path))

    if not available_files:
        raise HTTPException(
            status_code=404,
            detail="No Layer CSV diagnostic files are available yet.",
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    zip_filename = f"layer_csv_diagnostics_{timestamp}.zip"
    zip_path = os.path.join(tempfile.gettempdir(), zip_filename)

    try:
        with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            manifest = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "file_count": len(available_files),
                "files": [
                    {
                        "name": logical_name,
                        "filename": filename,
                        "size_bytes": path.stat().st_size,
                    }
                    for logical_name, filename, path in available_files
                ],
            }

            zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))

            for _logical_name, filename, path in available_files:
                zf.write(path, arcname=filename)

    except Exception as exc:
        logging.exception("[DevRoutes] Failed creating Layer CSV diagnostics ZIP")
        raise HTTPException(status_code=500, detail=str(exc))

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=zip_filename,
    )


@dev_routes.get("/layers/layer-routes")
@with_retries()
def layer_routes():
    return {
        "/layers/layer-routes": "Show Layer CSV route list",
        "/layers/cycles": "View parsed layer_cycles.csv rows as JSON",
        "/layers/cycles-csv": "Download layer_cycles.csv",
        "/layers/plans": "View parsed layer3_plans.csv rows as JSON",
        "/layers/plans-csv": "Download layer3_plans.csv",
        "/layers/orders": "View parsed layer4_orders.csv rows as JSON",
        "/layers/orders-csv": "Download layer4_orders.csv",

        "/layers/portfolio-snapshots": "View parsed layer_portfolio_snapshots.csv rows as JSON",
        "/layers/portfolio-snapshots-csv": "Download layer_portfolio_snapshots.csv",
        "/layers/shadow": "View parsed layer4_shadow.csv rows as JSON",
        "/layers/shadow-csv": "Download layer4_shadow.csv",

        "/layers/live-bar-health": "View parsed layer_live_bar_health.csv rows as JSON",
        "/layers/live-bar-health-csv": "Download layer_live_bar_health.csv",

        "/layers/live-strategy-shadow": "View parsed layer_live_strategy_shadow.csv rows as JSON",
        "/layers/live-strategy-shadow-csv": "Download layer_live_strategy_shadow.csv",

        "/layers/live-strategy-shadow-cycles": "View parsed layer_live_strategy_shadow_cycles.csv rows as JSON",
        "/layers/live-strategy-shadow-cycles-csv": "Download layer_live_strategy_shadow_cycles.csv",

        "/layers/live-strategy-outcomes": "View parsed layer_live_strategy_outcomes.csv rows as JSON",
        "/layers/live-strategy-outcomes-csv": "Download layer_live_strategy_outcomes.csv",

        "/layers/all-csv-diagnostics.zip": "Download all available Layer CSV diagnostics as one ZIP",
        "/layers/dashboard": "Summarize latest Layer cycle health, skips, plan state, and order state",
        "/layers/cycle/{cycle_id}": "Join cycle, plan, order, and snapshot rows for one Layer cycle",
        "/layers/symbol/{symbol}": "Summarize Layer 3 / Layer 4 behavior for one symbol",
        "/layers/effectiveness": "Summarize operational Layer effectiveness, skip reasons, execution rates, and symbol activity",
    }


@dev_routes.get("/layers/dashboard")
@with_retries()
def layer_dashboard(limit: int = Query(1000, ge=1, le=10000)):
    """
    Summarize latest Layer cycle health, skips, plan state, and order state.
    """
    cycles_data = _layer_csv_data("cycles", limit=limit)
    plans_data = _layer_csv_data("plans", limit=limit)
    orders_data = _layer_csv_data("orders", limit=limit)
    snapshots_data = _layer_csv_data("portfolio-snapshots", limit=limit)

    cycles = cycles_data.get("rows", []) or []
    plans = plans_data.get("rows", []) or []
    orders = orders_data.get("rows", []) or []
    snapshots = snapshots_data.get("rows", []) or []

    latest_cycle = cycles[-1] if cycles else None
    latest_plan_id = (
        latest_cycle.get("plan_id")
        if isinstance(latest_cycle, dict) and latest_cycle.get("plan_id")
        else (plans[-1].get("plan_id") if plans else None)
    )

    recent_cycles = cycles[-50:]
    skipped_cycles = [
        row
        for row in recent_cycles
        if str(row.get("status") or "").lower() not in {"ok", ""}
    ]

    latest_plan_rows = [
        row
        for row in plans
        if latest_plan_id and row.get("plan_id") == latest_plan_id
    ]

    latest_order_rows = [
        row
        for row in orders
        if latest_plan_id and row.get("plan_id") == latest_plan_id
    ]

    latest_snapshot_rows = [
        row
        for row in snapshots
        if latest_plan_id and row.get("plan_id") == latest_plan_id
    ]

    return {
        "status": "ok",
        "limit": limit,
        "files": {
            "cycles": {
                "status": cycles_data.get("status"),
                "count": cycles_data.get("count"),
                "returned": cycles_data.get("returned"),
            },
            "plans": {
                "status": plans_data.get("status"),
                "count": plans_data.get("count"),
                "returned": plans_data.get("returned"),
            },
            "orders": {
                "status": orders_data.get("status"),
                "count": orders_data.get("count"),
                "returned": orders_data.get("returned"),
            },
            "portfolio_snapshots": {
                "status": snapshots_data.get("status"),
                "count": snapshots_data.get("count"),
                "returned": snapshots_data.get("returned"),
            },
        },
        "latest_cycle": latest_cycle,
        "latest_plan_id": latest_plan_id,
        "latest_plan_row_count": len(latest_plan_rows),
        "latest_order_row_count": len(latest_order_rows),
        "latest_snapshot_row_count": len(latest_snapshot_rows),
        "recent_cycle_status_counts": _counter_to_dict(
            Counter(str(row.get("status") or "unknown") for row in recent_cycles)
        ),
        "recent_skip_reasons": _counter_to_dict(
            Counter(
                str(row.get("reason") or row.get("status") or "unknown")
                for row in skipped_cycles
            )
        ),
        "recent_layer4_blocked_reasons": _counter_to_dict(
            Counter(
                str(row.get("layer4_blocked_reason"))
                for row in recent_cycles
                if row.get("layer4_blocked_reason")
            )
        ),
        "latest_plan_decision_counts": _counter_to_dict(
            Counter(str(row.get("decision") or "unknown") for row in latest_plan_rows)
        ),
        "latest_order_status_counts": _counter_to_dict(
            Counter(str(row.get("status") or "unknown") for row in latest_order_rows)
        ),
    }


@dev_routes.get("/layers/cycle/{cycle_id}")
@with_retries()
def view_layer_cycle(cycle_id: str, limit: int = Query(10000, ge=1, le=10000)):
    """
    Join cycle, plan, order, and snapshot rows for one Layer cycle.
    """
    cycle_id = str(cycle_id).strip()

    cycles = [
        row
        for row in _layer_csv_rows("cycles", limit=limit)
        if _row_cycle_id(row) == cycle_id
    ]

    plans = [
        row
        for row in _layer_csv_rows("plans", limit=limit)
        if _row_cycle_id(row) == cycle_id
    ]

    orders = [
        row
        for row in _layer_csv_rows("orders", limit=limit)
        if _row_cycle_id(row) == cycle_id
    ]

    snapshots = [
        row
        for row in _layer_csv_rows("portfolio-snapshots", limit=limit)
        if _row_cycle_id(row) == cycle_id
    ]

    if not cycles and not plans and not orders and not snapshots:
        raise HTTPException(
            status_code=404,
            detail=f"No Layer CSV rows found for cycle_id={cycle_id}",
        )

    return {
        "status": "ok",
        "cycle_id": cycle_id,
        "counts": {
            "cycles": len(cycles),
            "plans": len(plans),
            "orders": len(orders),
            "portfolio_snapshots": len(snapshots),
        },
        "cycle_rows": cycles,
        "plan_rows": plans,
        "order_rows": orders,
        "portfolio_snapshot_rows": snapshots,
    }


@dev_routes.get("/layers/symbol/{symbol}")
@with_retries()
def view_layer_symbol(symbol: str, limit: int = Query(10000, ge=1, le=10000)):
    """
    Summarize Layer 3 / Layer 4 behavior for one symbol.
    """
    symbol = normalize_symbol(symbol)

    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    plans = [
        row
        for row in _layer_csv_rows("plans", limit=limit)
        if _row_symbol(row) == symbol
    ]

    orders = [
        row
        for row in _layer_csv_rows("orders", limit=limit)
        if _row_symbol(row) == symbol
    ]

    snapshots = [
        row
        for row in _layer_csv_rows("portfolio-snapshots", limit=limit)
        if _row_symbol(row) == symbol
    ]

    target_weights = [
        value
        for value in (
            safe_float(row.get("target_weight"), None)
            for row in plans
        )
        if value is not None
    ]

    latest_snapshot = snapshots[-1] if snapshots else None

    return {
        "status": "ok",
        "symbol": symbol,
        "counts": {
            "plan_rows": len(plans),
            "order_rows": len(orders),
            "portfolio_snapshot_rows": len(snapshots),
        },
        "decision_counts": _counter_to_dict(
            Counter(str(row.get("decision") or "unknown") for row in plans)
        ),
        "order_side_counts": _counter_to_dict(
            Counter(str(row.get("side") or "unknown") for row in orders)
        ),
        "order_status_counts": _counter_to_dict(
            Counter(str(row.get("status") or "unknown") for row in orders)
        ),
        "common_plan_reasons": _counter_to_dict(
            Counter(str(row.get("reason") or "unknown") for row in plans),
            limit=10,
        ),
        "common_order_reasons": _counter_to_dict(
            Counter(str(row.get("reason") or "unknown") for row in orders),
            limit=10,
        ),
        "average_target_weight": (
            round(sum(target_weights) / len(target_weights), 6)
            if target_weights else None
        ),
        "latest_snapshot": latest_snapshot,
        "recent_plan_rows": plans[-25:],
        "recent_order_rows": orders[-25:],
        "recent_portfolio_snapshot_rows": snapshots[-25:],
    }


@dev_routes.get("/layers/effectiveness")
@with_retries()
def layer_effectiveness(limit: int = Query(10000, ge=1, le=10000)):
    """
    Summarize operational Layer effectiveness, skip reasons, execution rates,
    and symbol activity.

    This does not calculate realized P/L. True trade performance requires
    fill/close tracking.
    """
    cycles = _layer_csv_rows("cycles", limit=limit)
    plans = _layer_csv_rows("plans", limit=limit)
    orders = _layer_csv_rows("orders", limit=limit)

    market_open_cycles = [
        row
        for row in cycles
        if _safe_bool_value(row.get("market_is_open"))
    ]

    skipped_cycles = [
        row
        for row in cycles
        if str(row.get("status") or "").lower() not in {"ok", ""}
    ]

    attempted = sum(safe_int(row.get("layer4_attempted"), 0) for row in cycles)
    submitted = sum(safe_int(row.get("layer4_submitted"), 0) for row in cycles)
    skipped = sum(safe_int(row.get("layer4_skipped"), 0) for row in cycles)
    errors = sum(safe_int(row.get("layer4_errors"), 0) for row in cycles)

    cash_pcts = [
        value
        for value in (
            safe_float(row.get("target_cash_pct"), None)
            for row in cycles
        )
        if value is not None
    ]

    blocked_by_counter = Counter()

    for row in plans:
        blocked_by = _json_value(row.get("blocked_by"), default=[])

        if isinstance(blocked_by, list):
            blocked_by_counter.update(str(item) for item in blocked_by if item)
        elif blocked_by:
            blocked_by_counter.update([str(blocked_by)])

    symbol_decisions = defaultdict(Counter)

    for row in plans:
        symbol = _row_symbol(row)
        decision = str(row.get("decision") or "unknown")

        if symbol:
            symbol_decisions[symbol].update([decision])

    return {
        "status": "ok",
        "limit": limit,
        "note": (
            "This is operational effectiveness, not realized P/L. "
            "True trade performance will require fill/close tracking such as layer4_fills.csv."
        ),
        "cycle_count": len(cycles),
        "market_open_cycle_count": len(market_open_cycles),
        "skipped_cycle_count": len(skipped_cycles),
        "cycle_status_counts": _counter_to_dict(
            Counter(str(row.get("status") or "unknown") for row in cycles)
        ),
        "skip_reason_counts": _counter_to_dict(
            Counter(
                str(row.get("reason") or row.get("status") or "unknown")
                for row in skipped_cycles
            )
        ),
        "layer3_decision_counts": _counter_to_dict(
            Counter(str(row.get("decision") or "unknown") for row in plans)
        ),
        "layer3_blocked_by_counts": _counter_to_dict(blocked_by_counter),
        "layer4_totals_from_cycles": {
            "attempted": attempted,
            "submitted": submitted,
            "skipped": skipped,
            "errors": errors,
            "submission_rate": round(submitted / attempted, 4) if attempted else None,
            "skip_rate": round(skipped / attempted, 4) if attempted else None,
            "error_rate": round(errors / attempted, 4) if attempted else None,
        },
        "layer4_blocked_reason_counts": _counter_to_dict(
            Counter(
                str(row.get("layer4_blocked_reason"))
                for row in cycles
                if row.get("layer4_blocked_reason")
            )
        ),
        "order_status_counts": _counter_to_dict(
            Counter(str(row.get("status") or "unknown") for row in orders)
        ),
        "most_common_order_symbols": _counter_to_dict(
            Counter(_row_symbol(row) for row in orders if _row_symbol(row)),
            limit=10,
        ),
        "symbol_decision_counts": {
            symbol: _counter_to_dict(counter)
            for symbol, counter in sorted(symbol_decisions.items())
        },
        "average_target_cash_pct": (
            round(sum(cash_pcts) / len(cash_pcts), 6)
            if cash_pcts else None
        ),
    }


@dev_routes.get("/layers/{name}-csv")
@with_retries()
def download_layer_csv(name: str):
    filename = _layer_csv_filename(name)
    path = layer_csv_path(filename)

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"{filename} not found",
        )

    return FileResponse(
        path,
        media_type="text/csv",
        filename=filename,
    )


@dev_routes.get("/layers/{name}")
@with_retries()
def view_layer_csv(name: str, limit: int = Query(1000, ge=1, le=10000)):
    filename = _layer_csv_filename(name)
    return read_csv_rows(filename, limit=limit)


# ================================================================
# ACTION ROUTES — MANUAL EXECUTION
# ---------------------------------------------------------------
# Routes that can force debug buy/sell behavior through the execution path.
# Use carefully while the bot is live.
# ================================================================


@dev_routes.post("/debug-buy")
@with_retries()
async def debug_buy(symbol: str = "AAPL", price: float = 100.0):
    """
    Force a debug buy through the execution path.
    """
    try:
        manager = get_stream_manager()
        app_state.setdefault("routes", {}).setdefault("dev_routes", {})["test_run"] = True

        await manager._execute_buy(symbol, price)

        return {
            "message": "Debug buy executed",
            "symbol": symbol,
            "price": price,
        }

    except Exception as e:
        logging.exception("[DevRoutes] debug-buy failed")
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        app_state.setdefault("routes", {}).setdefault("dev_routes", {})["test_run"] = False


@dev_routes.post("/debug-sell")
@with_retries()
async def debug_sell(symbol: str = "AAPL", price: float = 100.0):
    """
    Force a debug sell through the execution path.
    """
    try:
        manager = get_stream_manager()
        app_state.setdefault("routes", {}).setdefault("dev_routes", {})["test_run"] = True

        await manager._execute_sell(symbol, price)

        return {
            "message": "Debug sell executed",
            "symbol": symbol,
            "price": price,
        }

    except Exception as e:
        logging.exception("[DevRoutes] debug-sell failed")
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        app_state.setdefault("routes", {}).setdefault("dev_routes", {})["test_run"] = False


# ================================================================
# ACTION ROUTES — TRADE SIMULATION
# ---------------------------------------------------------------
# Routes that inject fake trade ticks or simulated test sequences into
# the stream handler. Use carefully during live market validation.
# ================================================================


@dev_routes.post("/simulate-buy")
@with_retries()
async def simulate_buy(symbol: str = "AAPL", price: float = 100.0):
    """
    Inject a fake trade tick into the normal trade handler.
    """
    try:
        manager = get_stream_manager()
        fake_trade = FakeTrade(price=price, symbol=symbol)
        await manager._handle_trade(fake_trade)

        return {
            "message": "Simulated buy tick processed",
            "symbol": symbol,
            "price": price,
        }

    except Exception as e:
        logging.exception("[DevRoutes] simulate-buy failed")
        raise HTTPException(status_code=500, detail=str(e))


@dev_routes.post("/simulate-sell")
@with_retries()
async def simulate_sell(symbol: str = "AAPL", price: float = 100.0):
    """
    Inject a fake trade tick into the normal trade handler.
    """
    try:
        manager = get_stream_manager()
        fake_trade = FakeTrade(price=price, symbol=symbol)
        await manager._handle_trade(fake_trade)

        return {
            "message": "Simulated sell tick processed",
            "symbol": symbol,
            "price": price,
        }

    except Exception as e:
        logging.exception("[DevRoutes] simulate-sell failed")
        raise HTTPException(status_code=500, detail=str(e))


@dev_routes.post("/simulate-100-trades")
@with_retries()
async def simulate_100_trades(symbol: str = "AAPL", start_price: float = 100.0):
    """
    Run a repeated fake-trade loop for stress testing.
    """
    results = []

    try:
        manager = get_stream_manager()
        price = start_price

        for i in range(100):
            fake_trade = FakeTrade(price=price, symbol=symbol)
            await manager._handle_trade(fake_trade)

            results.append(
                {
                    "index": i + 1,
                    "price": price,
                    "symbol": symbol,
                }
            )
            price += 0.25

        return {"message": "100 simulated trades complete", "results": results}

    except Exception as e:
        logging.exception("[DevRoutes] simulate-100-trades failed")
        raise HTTPException(status_code=500, detail=str(e))


@dev_routes.post("/test-sequence")
@with_retries()
async def test_sequence(symbol: str = "AAPL", start_price: float = 100.0):
    """
    Run a minimal fake tick sequence.
    """
    try:
        manager = get_stream_manager()

        buy_tick = FakeTrade(price=start_price, symbol=symbol)
        await manager._handle_trade(buy_tick)

        sell_tick = FakeTrade(price=start_price + 1.0, symbol=symbol)
        await manager._handle_trade(sell_tick)

        return {
            "message": "Test sequence complete",
            "symbol": symbol,
            "start_price": start_price,
            "end_price": start_price + 1.0,
        }

    except Exception as e:
        logging.exception("[DevRoutes] test-sequence failed")
        raise HTTPException(status_code=500, detail=str(e))


@dev_routes.post("/test-buy-only")
@with_retries()
async def test_buy_only(symbol: str = "AAPL", price: float = 100.0):
    """
    Inject a single fake trade tick.
    """
    try:
        manager = get_stream_manager()
        await manager._handle_trade(FakeTrade(price=price, symbol=symbol))

        return {
            "message": "Buy-only test complete",
            "symbol": symbol,
            "price": price,
        }

    except Exception as e:
        logging.exception("[DevRoutes] test-buy-only failed")
        raise HTTPException(status_code=500, detail=str(e))


@dev_routes.post("/test-sell-only")
@with_retries()
async def test_sell_only(symbol: str = "AAPL", price: float = 100.0):
    """
    Inject a single fake trade tick.
    """
    try:
        manager = get_stream_manager()
        await manager._handle_trade(FakeTrade(price=price, symbol=symbol))

        return {
            "message": "Sell-only test complete",
            "symbol": symbol,
            "price": price,
        }

    except Exception as e:
        logging.exception("[DevRoutes] test-sell-only failed")
        raise HTTPException(status_code=500, detail=str(e))


# ================================================================
# ACTION ROUTES — ALERTS / BALANCE TESTING
# ---------------------------------------------------------------
# Routes that send test alerts or force one account balance refresh.
# These are not trading routes, but they still trigger side effects.
# ================================================================


@dev_routes.post("/test-alert")
@with_retries()
def test_alert():
    """
    Send a test email alert.
    """
    logging.info(
        "📤 Sending test alert | recipients=%s telegram_configured=%s telegram_chat_ids_configured=%s",
        len(config.EMAIL_RECIPIENTS or []),
        bool(config.TELEGRAM_BOT_TOKEN),
        bool(config.TELEGRAM_CHAT_ID),
    )

    send_email_alert("✅ Test Alert", "This is a test email alert from dev routes.")
    return {"message": "Test email alert sent"}


@dev_routes.post("/test-telegram")
@with_retries()
def test_telegram_alert():
    """
    Send a test Telegram alert.
    """
    send_telegram_alert("✅ Test Telegram alert from dev routes.")
    return {"message": "Test Telegram alert sent"}


@dev_routes.post("/test-update-balance")
@with_retries()
async def test_update_balance():
    """
    Force one balance update cycle.
    """
    balance_tracker = app_state["services"]["balance_tracker"]["instance"]
    if balance_tracker is None:
        raise HTTPException(status_code=500, detail="Balance tracker is not initialized")

    await balance_tracker.update_balance()
    return {
        "message": "Balance updated",
        "balance": app_state["services"]["balance_tracker"]["balance"],
        "equity": app_state["services"]["balance_tracker"]["equity"],
    }