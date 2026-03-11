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
    Syncs held positions from Alpaca to app_state['open_trades']
    if not already populated. This prevents runtime errors due to missing trade info.
    """
    try:
        positions = app_state["trading_client"].get_all_positions()

        logging.debug(f"[Sync] Fetched {len(positions)} Alpaca positions")
        logging.debug(f"[Sync] Existing app_state['open_trades']: {app_state.get('open_trades')}")

        open_trades = app_state.setdefault("open_trades", {})
        
        if not positions:
            logging.info("[Sync] No positions reported by Alpaca. Clearing open_trades.")
            open_trades.clear()
            return

        for pos in positions:
            symbol = pos.symbol
            qty = float(pos.qty)
            if qty > 0:
                if symbol not in open_trades:
                    logging.warning(f"⚠️ No trade info found for held position. Adding {symbol} to open_trades.")
                    open_trades[symbol] = {
                        "buy_price": float(pos.avg_entry_price),
                        "buy_time": datetime.now(timezone.utc),  # Approximate
                        "order_id": None  # Unknown — left as None
                    }

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
