# dev_routes.py

import os
import csv
import io

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse

from state import app_state
from utils import config_utils as config
from utils.alerts_utils import send_email_alert, send_telegram_alert
from utils.trade_utils import TRADE_REASON_LOG
from utils.auth_utils import verify_credentials
from utils.misc_utils import with_retries
from stream import FakeTrade

# ================================================================
# dev_routes.py
#
# Development and debugging endpoints used for testing
# trading logic, system behavior, and diagnostics.
#
# These routes should NOT be exposed in production
# environments unless properly secured.
#
# Responsibilities
# ---------------------------------------------------------------
# • Trade simulation
# • Debugging trading behavior
# • Strategy diagnostics
# • Manual alert testing
#
# ================================================================
#
# ROUTE TABLE
#
# ┌────────────────────────┬────────┬────────────────────────────────────┐
# │ Route                  │ Method │ Description                        │
# ├────────────────────────┼────────┼────────────────────────────────────┤
# │ /dev-routes            │ GET    │ List dev/debug routes              │
# ├────────────────────────┼────────┼────────────────────────────────────┤
# │ /debug-buy             │ POST   │ Force debug buy                    │
# │ /debug-sell            │ POST   │ Force debug sell                   │
# ├────────────────────────┼────────┼────────────────────────────────────┤
# │ /simulate-buy          │ POST   │ Simulate buy execution             │
# │ /simulate-sell         │ POST   │ Simulate sell execution            │
# │ /simulate-100-trades   │ POST   │ Run 100 simulated trades           │
# ├────────────────────────┼────────┼────────────────────────────────────┤
# │ /test-sequence         │ POST   │ Run buy → sell test sequence       │
# │ /test-buy-only         │ POST   │ Trigger buy test                   │
# │ /test-sell-only        │ POST   │ Trigger sell test                  │
# ├────────────────────────┼────────┼────────────────────────────────────┤
# │ /test-positions        │ GET    │ Show Alpaca positions              │
# │ /debug/open-trades     │ GET    │ Inspect open_trades state          │
# ├────────────────────────┼────────┼────────────────────────────────────┤
# │ /trades                │ GET    │ View trade log                     │
# │ /trades-csv            │ GET    │ Export trade log CSV               │
# │ /trade-decisions       │ GET    │ View strategy decision state       │
# ├────────────────────────┼────────┼────────────────────────────────────┤
# │ /diagnostics           │ GET    │ Run diagnostics snapshot           │
# ├────────────────────────┼────────┼────────────────────────────────────┤
# │ /test-alert            │ POST   │ Send test email alert              │
# │ /test-telegram         │ POST   │ Send test Telegram alert           │
# │ /test-update-balance   │ POST   │ Force balance refresh              │
# └────────────────────────┴────────┴────────────────────────────────────┘
#
# Notes
# ---------------------------------------------------------------
# • Router protected by authentication
# • Intended for development/testing only
# • May trigger trades or alerts
#
# ================================================================

dev_routes = APIRouter(dependencies=[Depends(verify_credentials)])


def get_stream_manager():
    manager = app_state.get("stream", {}).get("manager")
    if not manager:
        raise HTTPException(status_code=503, detail="Stream manager is not available")
    return manager

# ================================================================
# ROUTE DISCOVERY
# ================================================================

# ================================================================
# ROUTE DISCOVERY
# ================================================================

@dev_routes.get("/dev-routes", response_class=HTMLResponse)
def dev_route_list():
    """
    Return a simple HTML list of dev/debug routes.
    """
    route_descriptions = {
        "/dev-routes": "This dev route list",
        "/debug-buy": "Force a debug buy through execution path",
        "/debug-sell": "Force a debug sell through execution path",
        "/simulate-buy": "Inject a fake trade tick",
        "/simulate-sell": "Inject a fake trade tick",
        "/simulate-100-trades": "Stress test trade flow with fake ticks",
        "/test-positions": "Show current positions",
        "/test-sequence": "Run a simple fake tick sequence test",
        "/test-buy-only": "Inject a single fake trade tick",
        "/test-sell-only": "Inject a single fake trade tick",
        "/trades": "View completed trades",
        "/trades-csv": "Download trade log CSV",
        "/trade-decisions": "View trade decision log",
        "/diagnostics": "Run system diagnostics",
        "/test-alert": "Send a test email alert",
        "/test-telegram": "Send a test Telegram alert",
        "/test-update-balance": "Force one balance update",
        "/debug/open-trades": "View current open_trades state",
    }

    html = "<html><body><h1>Dev Routes</h1><ul>"
    for path, desc in route_descriptions.items():
        html += f"<li><code>{path}</code> — {desc}</li>"
    html += "</ul></body></html>"

    return HTMLResponse(content=html)


# ================================================================
# DEBUG TRADING ACTIONS
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
# TRADE SIMULATION
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


# ================================================================
# POSITION / STATE INSPECTION
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


# ================================================================
# TRADE LOGS / DECISIONS
# ================================================================

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


@dev_routes.get("/trade-decisions")
@with_retries()
def view_trade_decisions():
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

@dev_routes.get("/trade-history")
@with_retries()
def view_trade_history():
    """
    Return parsed rows from trade_history.csv as JSON.
    """
    path = app_state.get("paths", {}).get("TRADE_HISTORY_FILE", "trade_history.csv")

    if not os.path.exists(path):
        return {
            "status": "missing",
            "file": path,
            "rows": [],
            "count": 0,
            "message": "trade_history.csv not found"
        }

    rows = []
    with open(path, mode="r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    return {
        "status": "ok",
        "file": path,
        "count": len(rows),
        "rows": rows
    }


@dev_routes.get("/trade-history-csv", response_class=PlainTextResponse)
@with_retries()
def download_trade_history_csv():
    """
    Return raw trade_history.csv contents.
    """
    path = app_state.get("paths", {}).get("TRADE_HISTORY_FILE", "trade_history.csv")

    if not os.path.exists(path):
        return f"file_not_found,{path}"

    with open(path, mode="r", encoding="utf-8") as f:
        return f.read()


@dev_routes.get("/trade-summary")
@with_retries()
def view_trade_summary():
    """
    Return parsed rows from trade_summary.csv as JSON.
    """
    path = app_state.get("paths", {}).get("TRADE_SUMMARY_FILE", "trade_summary.csv")

    if not os.path.exists(path):
        return {
            "status": "missing",
            "file": path,
            "rows": [],
            "count": 0,
            "message": "trade_summary.csv not found"
        }

    rows = []
    with open(path, mode="r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    return {
        "status": "ok",
        "file": path,
        "count": len(rows),
        "rows": rows
    }


@dev_routes.get("/trade-summary-csv", response_class=PlainTextResponse)
@with_retries()
def download_trade_summary_csv():
    """
    Return raw trade_summary.csv contents.
    """
    path = app_state.get("paths", {}).get("TRADE_SUMMARY_FILE", "trade_summary.csv")

    if not os.path.exists(path):
        return f"file_not_found,{path}"

    with open(path, mode="r", encoding="utf-8") as f:
        return f.read()


@dev_routes.get("/trade-decisions-csv", response_class=PlainTextResponse)
@with_retries()
def download_trade_decisions_csv():
    """
    Return raw trade_decisions_log.csv contents.
    """
    path = TRADE_REASON_LOG

    if not os.path.exists(path):
        return f"file_not_found,{path}"

    with open(path, mode="r", encoding="utf-8") as f:
        return f.read()


@dev_routes.get("/trade-decisions-history")
@with_retries()
def view_trade_decisions_history():
    """
    Return parsed rows from trade_decisions_log.csv as JSON.
    """
    path = TRADE_REASON_LOG

    if not os.path.exists(path):
        return {
            "status": "missing",
            "file": path,
            "rows": [],
            "count": 0,
            "message": "trade_decisions_log.csv not found"
        }

    rows = []
    with open(path, mode="r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    return {
        "status": "ok",
        "file": path,
        "count": len(rows),
        "rows": rows
    }


# ================================================================
# TEST SEQUENCES
# ================================================================

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
# DIAGNOSTICS
# ================================================================

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
# ALERT TESTING
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