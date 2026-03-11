import asyncio
from datetime import datetime, timezone
import time
import logging
from state import app_state, app_state_lock, fail_safe_event
from utils.alerts_utils import send_email_alert
from utils.trade_utils import log_trade_to_csv, log_trade_to_summary
from utils import config_utils as config
from utils.config_utils import get_config

# === Fail-Safe Actions ===
async def sell_position(symbol, price=None):
    """
    Forces a sell of the given symbol if a position exists.
    Safe against duplicate forced-liquidation attempts.
    """
    liquidation_in_progress = app_state["fail_safes"].setdefault("liquidation_in_progress", set())

    if app_state["stream"]["shutdown_event"].is_set():
        logging.warning(f"[SELL_POSITION] Shutdown active; refusing forced sell for {symbol}")
        return

    if symbol in liquidation_in_progress:
        logging.warning(f"[SELL_POSITION] Forced liquidation already in progress for {symbol}")
        return

    async with app_state["fail_safes"]["position_lock"]:
        if symbol not in app_state.get("open_trades", {}):
            logging.warning(f"[SELL_POSITION] No open trade found for {symbol}. Aborting sell.")
            liquidation_in_progress.discard(symbol)
            return

        stream_manager = app_state["stream"].get("manager")
        if not stream_manager:
            logging.error("[SELL_POSITION] Stream manager not available. Cannot execute sell.")
            return

        liquidation_in_progress.add(symbol)

        try:
            live_price = app_state.get("last_trade_price_by_symbol", {}).get(symbol)
            if live_price and live_price > 0:
                price = live_price

            if not price or price <= 0:
                price = app_state["open_trades"].get(symbol, {}).get("buy_price", 0)

            logging.info(f"[SELL_POSITION] 🚨 Forced liquidation for {symbol} at ${price:.2f}")

            app_state["strategy"].setdefault("last_sell", {})[symbol] = {
                "price": price,
                "time": datetime.now(timezone.utc),
                "entry_price": app_state["open_trades"].get(symbol, {}).get("buy_price", price),
                "reason": "fail_safe",
            }

            await stream_manager._execute_sell(symbol, price)
            logging.info(f"[SELL_POSITION] ✅ Forced sell submitted/completed for {symbol}")

        except asyncio.TimeoutError:
            logging.error(f"[SELL_POSITION] ⌛ Sell timeout for {symbol}")
        except Exception as e:
            logging.error(f"[SELL_POSITION] ❌ Error during forced sell of {symbol}: {e}")
        finally:
            liquidation_in_progress.discard(symbol)

async def check_global_fail_safe():
    """
    Triggers a forced sell of all open positions if account equity drops below threshold.
    """
    tracker = app_state["services"].get("balance_tracker", {}).get("instance")
    if not tracker:
        logging.warning("⚠️ No balance tracker available for fail-safe check.")
        return

    info = tracker.get_balance()
    equity = info.get("equity", 0)
    equity_threshold = get_config("EQUITY_THRESHOLD")

    logging.debug(f"[FailSafe] Checking equity: ${equity:.2f} vs threshold: ${equity_threshold:.2f}")

    if equity < equity_threshold:

        if app_state["fail_safes"].get("state"):
            logging.debug("[FailSafe] Global fail-safe already active.")
            return

        logging.warning(f"⚠️ Global fail-safe triggered! Equity below {equity_threshold}")

        if not app_state["fail_safes"].get("email_suppressed", False):
            send_email_alert(
                "Global Fail-Safe Triggered",
                f"Equity dropped to ${equity:.2f}. Selling all positions."
            )

        with app_state_lock:
            app_state["fail_safes"]["state"] = True
            app_state["fail_safes"]["updated_at"] = time.time()
            app_state["fail_safes"]["last_trigger_reason"] = "global"

        fail_safe_event.set()

        open_trades_snapshot = dict(app_state.get("open_trades", {}))
        for symbol, trade_data in open_trades_snapshot.items():
            price = app_state.get("last_trade_price_by_symbol", {}).get(symbol) or trade_data.get("buy_price", 0)
            await sell_position(symbol, price)


async def check_per_stock_fail_safe():

    with app_state_lock:
        fs = app_state.setdefault("fail_safes", {})
        cache = fs.setdefault("invalid_price_cache", {})
        fs.setdefault("state", False)

        open_trades_snapshot = dict(app_state.get("open_trades", {}))
        last_price_snapshot = dict(app_state.get("last_trade_price_by_symbol", {}))

    for symbol, trade_data in open_trades_snapshot.items():
        entry_price = trade_data.get("buy_price", 0)
        current_price = last_price_snapshot.get(symbol, 0)

        logging.debug(f"[FailSafe] {symbol}: Entry=${entry_price}, Current=${current_price}")

        # --- invalid price cache logic (guard writes with the lock) ---
        with app_state_lock:
            last_cached = cache.get(symbol)
            
        if entry_price <= 0 or current_price <= 0:
            with app_state_lock:
                if cache.get(symbol) == current_price:
                    continue
                cache[symbol] = current_price

            logging.error(
                f"[FailSafe] Invalid buy_price ({entry_price}) or current price ({current_price}) for {symbol}"
            )
            continue
        else:
            # Price recovered; clear cache for this symbol
            if last_cached is not None:
                with app_state_lock:
                    cache.pop(symbol, None)

        percent_loss = ((entry_price - current_price) / entry_price) * 100
        threshold = get_config("MAX_POSITION_LOSS_PERCENT")
        logging.debug(f"[FailSafe] {symbol} loss: {percent_loss:.2f}% (Threshold: {threshold}%)")
        
        if percent_loss >= threshold:
            logging.warning(f"⚠️ Fail-safe triggered for {symbol}! Loss: {percent_loss:.2f}%")
            send_email_alert(
                "Per-Stock Fail-Safe Triggered",
                f"{symbol} dropped {percent_loss:.2f}%. Selling position."
            )
            log_trade_to_csv(symbol, "FAILSAFE", current_price, time.strftime("%Y-%m-%d %H:%M:%S"))

            with app_state_lock:
                fs["state"] = True
                fs["symbol"] = symbol  # keep for backward compatibility / latest trigger
                fs.setdefault("symbols", set()).add(symbol)
                fs["updated_at"] = time.time()
                fs["last_trigger_reason"] = "per_stock"

            fail_safe_event.set()
            logging.error(f"[FailSafe] state=True set at {time.strftime('%H:%M:%S')}")

            await sell_position(symbol, current_price)

async def _sleep_with_shutdown(seconds: float, step: float = 0.25) -> None:
    """
    Sleep in small chunks so shutdown_event can interrupt quickly.
    Uses app_state["stream"]["shutdown_event"] (threading.Event).
    """
    end = time.time() + float(seconds)
    while time.time() < end:
        if app_state["stream"]["shutdown_event"].is_set():
            return
        await asyncio.sleep(min(step, end - time.time()))

async def monitor_fail_safes():
    """
    Background loop that continuously monitors for global and per-stock fail-safe triggers.
    Stops cleanly when shutdown_event is set.
    """

    shutdown_event = app_state["stream"].get("shutdown_event")

    while not shutdown_event.is_set():

        if app_state["stream"].get("state") != "running":
            logging.debug("[FailSafe] Skipping failsafe check — stream not active.")
            await _sleep_with_shutdown(5)
            continue

        try:
            # Reset email suppression at midnight UTC
            if app_state["fail_safes"].get("email_suppressed"):
                reset_day = app_state["fail_safes"].get("email_suppression_reset")
                if reset_day and datetime.now(timezone.utc).date() >= reset_day:
                    with app_state_lock:
                        app_state["fail_safes"]["email_suppressed"] = False
                        app_state["fail_safes"]["email_suppression_reset"] = None
                    logging.info("📬 Email alerts re-enabled after midnight UTC reset.")

            await check_global_fail_safe()
            await check_per_stock_fail_safe()

            await _sleep_with_shutdown(10)

        except Exception as e:
            logging.error(f"Error in fail-safe monitor: {e}")
            await _sleep_with_shutdown(10)

    logging.info("[FailSafe] monitor_fail_safes exiting due to shutdown_event.")