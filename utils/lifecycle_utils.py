import os
import time
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from state import app_state
from config import PROGRAM_STARTUP_FILE, PROGRAM_SHUTDOWN_FILE, PROGRAM_SHUTDOWN_REASON_FILE

from utils.threading_utils import safe_thread
from utils.alerts_utils import send_email_alert

def record_program_startup():
    try:
        with open(app_state["paths"]["PROGRAM_STARTUP_FILE"], "w") as f:
            f.write(str(time.time()))
        logging.info("✅ Program startup time recorded.")
    except Exception as e:
        logging.error(f"Failed to record program startup: {e}")

def record_program_shutdown(reason="clean"):
    try:
        with open(app_state["paths"]["PROGRAM_SHUTDOWN_FILE"], "w") as f:
            f.write(str(time.time()))
        with open(app_state["paths"]["PROGRAM_SHUTDOWN_REASON_FILE"], "w") as f:
            f.write(reason)
        logging.info(f"🛑 Program shutdown recorded. Reason: {reason}")
    except Exception as e:
        logging.error(f"Failed to record program shutdown: {e}")

def auto_restart_if_abnormal_shutdown():
    abnormal, reason = was_last_program_shutdown_abnormal()
    if abnormal:
        logging.warning(f"🔁 Last shutdown abnormal: {reason}. Restarting stream...")
        send_email_alert("🔁 Auto-Restart Triggered", f"Restarting trade stream due to abnormal shutdown: {reason}")
        stream = app_state["stream"].get("instance")
        if stream:
            safe_thread(stream.start, name="TradeStreamAutoRestart")
        else:
            logging.error("❌ No stream instance found in app_state — cannot auto-restart.")
    else:
        logging.info(f"🟢 Last shutdown was clean ({reason}). No restart needed.")

def was_last_program_shutdown_abnormal():
    try:
        reason_path = app_state["paths"]["PROGRAM_SHUTDOWN_REASON_FILE"]
        if not os.path.exists(reason_path):
            return True, "No shutdown reason recorded"

        with open(reason_path) as f:
            reason = f.read().strip()

        return reason != "clean", reason
    except Exception as e:
        logging.error(f"Failed to check last shutdown status: {e}")
        return True, "Unknown error"

def sync_open_positions_to_app_state(app_state):
    """
    Sync held broker positions into app_state['open_trades'] if needed.

    These entries are marked as status='synced' because they were reconstructed
    from live Alpaca positions rather than created by this process during a
    fresh local order lifecycle.
    """
    try:
        positions = app_state["trading_client"].get_all_positions()

        logging.debug(f"[Sync] Fetched {len(positions)} Alpaca positions")
        logging.debug(f"[Sync] Existing app_state['open_trades']: {app_state.get('open_trades')}")

        open_trades = app_state.setdefault("open_trades", {})
        live_position_symbols = set()

        if not positions:
            logging.info("[Sync] No positions reported by Alpaca. Clearing open_trades.")
            open_trades.clear()
            return

        for pos in positions:
            symbol = str(getattr(pos, "symbol", "")).upper().strip()
            qty = float(getattr(pos, "qty", 0) or 0)

            if qty <= 0 or not symbol:
                continue

            live_position_symbols.add(symbol)

            existing = open_trades.get(symbol)
            avg_entry_price = float(getattr(pos, "avg_entry_price", 0) or 0)

            if not isinstance(existing, dict):
                logging.warning(f"⚠️ No trade info found for held position. Adding {symbol} to open_trades.")
                open_trades[symbol] = {
                    "buy_price": avg_entry_price,
                    "buy_time": datetime.now(timezone.utc),  # approximate reconstruction time
                    "quantity": qty,
                    "status": "synced",
                    "order_id": None,
                    "max_price": avg_entry_price,
                }
                continue

            existing_status = str(existing.get("status", "")).lower().strip()

            # If the entry exists but is not a real active held state, repair it.
            if existing_status not in {"filled", "pending_sell", "synced"}:
                logging.warning(
                    f"[Sync] Repairing {symbol} local trade state from status={existing_status!r} to synced."
                )
                open_trades[symbol] = {
                    "buy_price": avg_entry_price,
                    "buy_time": existing.get("buy_time", datetime.now(timezone.utc)),
                    "quantity": qty,
                    "status": "synced",
                    "order_id": existing.get("order_id"),
                    "max_price": max(float(existing.get("max_price", avg_entry_price) or avg_entry_price), avg_entry_price),
                }
            else:
                # Keep valid active states, but patch missing fields if needed
                existing["buy_price"] = float(existing.get("buy_price", avg_entry_price) or avg_entry_price)
                existing["buy_time"] = existing.get("buy_time", datetime.now(timezone.utc))
                existing["quantity"] = float(existing.get("quantity", qty) or qty)
                existing["status"] = existing_status or "synced"
                existing["order_id"] = existing.get("order_id")
                existing["max_price"] = float(existing.get("max_price", existing["buy_price"]) or existing["buy_price"])

        # Remove stale local entries for symbols no longer held at broker,
        # but only if they are supposed to represent held positions.
        for symbol in list(open_trades.keys()):
            trade_info = open_trades.get(symbol)
            if not isinstance(trade_info, dict):
                continue

            status = str(trade_info.get("status", "")).lower().strip()
            if status in {"filled", "pending_sell", "synced"} and symbol not in live_position_symbols:
                logging.warning(f"[Sync] Removing stale local open_trades entry for {symbol}; broker shows no live position.")
                open_trades.pop(symbol, None)

    except Exception as e:
        logging.error(f"❌ Failed to sync open positions: {e}")

def should_send_startup_alert():
    try:
        path = pathlib.Path(app_state["paths"]["PROGRAM_STARTUP_FILE"])
        if path.exists():
            last_time = datetime.fromtimestamp(float(path.read_text().strip()))
            if datetime.now() - last_time < timedelta(minutes=10):
                return False
        path.write_text(str(time.time()))
    except Exception as e:
        logging.warning(f"Startup alert timing check failed: {e}")
    return True
