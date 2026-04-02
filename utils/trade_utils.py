import os
import csv
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from utils.alerts_utils import send_email_alert
from utils.config_utils import get_config
from state import app_state
import asyncio
from collections import deque



TRADE_REASON_LOG = "trade_decisions_log.csv"

# ─────────────────────────────────────────────
# Trade decision log field dictionary
# ─────────────────────────────────────────────
#
# decision_stage:
#   candidate          -> strategy evaluation started for this symbol/tick
#   vetoed             -> blocked by one or more hard veto strategies
#   threshold_pass     -> confidence threshold passed and a tentative signal was chosen
#   execution_blocked  -> signal existed but execution was blocked (cooldown, min_hold, etc.)
#   execution_override -> execution forced by override logic such as failsafe
#   executed           -> trade signal was accepted for execution
#   no_signal          -> no side won threshold/arbitration
#   skipped            -> evaluation completed but no trade was taken
#
# decision_reason:
#   buy_only                  -> buy threshold passed, sell threshold did not
#   sell_only                 -> sell threshold passed, buy threshold did not
#   buy_wins_conflict         -> both crossed, but buy edge won by conflict margin
#   sell_wins_conflict        -> both crossed, but sell edge won by conflict margin
#   conflict_blocked_flat     -> both crossed while flat, but neither side won enough
#   conflict_blocked_in_position -> both crossed in position, but neither side won enough
#   no_flat_entry             -> flat but no valid buy entry
#   no_exit                   -> holding but no valid sell exit
#   cooldown                  -> blocked by per-symbol cooldown
#   min_hold                  -> blocked by minimum hold requirement
#   missing_trade_info        -> sell blocked because local trade state was missing
#   no_position               -> sell blocked because no tracked/local position existed
#   cannot_sell               -> sell signal existed but sell path was not allowed
#   failsafe_trigger          -> forced sell due to failsafe override
#   hard_veto                 -> blocked by one or more hard veto strategies
#
# buy_edge:
#   buy_confidence - abs(sell_confidence)
#   Positive means buy side dominates.
#
# sell_edge:
#   abs(sell_confidence) - buy_confidence
#   Positive means sell side dominates.
#
# buy_threshold / sell_threshold:
#   Actual thresholds used for this decision after any volatility-based adjustment.
#
# trigger_strategies:
#   Compact list of signal strategies that were active for this evaluation.
#
# veto_strategies:
#   Compact list of veto/filter reasons that blocked execution or trade selection.

def log_trade_decision(
    symbol,
    action,
    price,
    result,
    confidence=None,
    triggers=None,
    vetoes=None,
    volatility=None,
    strategy_results=None,
    decision_stage=None,
    decision_reason=None,
    buy_conf=None,
    sell_conf=None,
    buy_edge=None,
    sell_edge=None,
    buy_threshold=None,
    sell_threshold=None,
):
    triggers = ", ".join(triggers or [])
    vetoes = ", ".join(vetoes or [])
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    file_exists = os.path.exists(TRADE_REASON_LOG)

    try:
        sr_json = json.dumps(strategy_results or {}, separators=(",", ":"), default=str)
    except Exception:
        sr_json = ""

    with open(TRADE_REASON_LOG, mode="a", newline="") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow([
                "timestamp",
                "symbol",
                "action",
                "price",
                "result",
                "confidence",
                "decision_stage",
                "decision_reason",
                "buy_conf",
                "sell_conf",
                "buy_edge",
                "sell_edge",
                "buy_threshold",
                "sell_threshold",
                "trigger_strategies",
                "veto_strategies",
                "volatility",
                "strategy_results_json",
            ])

        writer.writerow([
            timestamp,
            symbol,
            action or "",
            f"{float(price):.2f}" if price is not None else "",
            result or "",
            f"{float(confidence):.4f}" if confidence is not None else "",
            decision_stage or "",
            decision_reason or "",
            f"{float(buy_conf):.4f}" if buy_conf is not None else "",
            f"{float(sell_conf):.4f}" if sell_conf is not None else "",
            f"{float(buy_edge):.4f}" if buy_edge is not None else "",
            f"{float(sell_edge):.4f}" if sell_edge is not None else "",
            f"{float(buy_threshold):.4f}" if buy_threshold is not None else "",
            f"{float(sell_threshold):.4f}" if sell_threshold is not None else "",
            triggers,
            vetoes,
            f"{float(volatility):.4f}" if volatility is not None else "",
            sr_json,
        ])


def ensure_trade_logs_exist():
    # --- Trade summary log ---
    summary_path = app_state["paths"]["TRADE_SUMMARY_FILE"]
    if not os.path.exists(summary_path):
        with open(summary_path, mode="w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Symbol",
                "Buy Price",
                "Buy Time",
                "Sell Price",
                "Sell Time",
                "P/L",
                "Holding Time (s)",
                "Trade Type",
            ])

    # --- Trade history log ---
    history_path = app_state["paths"]["TRADE_HISTORY_FILE"]
    if not os.path.exists(history_path):
        with open(history_path, mode="w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Timestamp",
                "Symbol",
                "Action",
                "Price",
                "Quantity",
                "Order ID",
            ])

    # --- Trade decision log ---
    if not os.path.exists(TRADE_REASON_LOG):
        with open(TRADE_REASON_LOG, mode="w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp",
                "symbol",
                "action",
                "price",
                "result",
                "confidence",
                "decision_stage",
                "decision_reason",
                "buy_conf",
                "sell_conf",
                "buy_edge",
                "sell_edge",
                "buy_threshold",
                "sell_threshold",
                "trigger_strategies",
                "veto_strategies",
                "volatility",
                "strategy_results_json",
            ])

def is_market_open(clock):
    now = datetime.now(timezone.utc)
    buffer = timedelta(minutes=1)
    return (clock.next_open - buffer) <= now <= (clock.next_close + buffer)


async def check_and_update_trade_window(now):
    trade_state = app_state["utils"]["trades_utils"]
    trade_timestamps = trade_state["trade_timestamps"]
    trade_timestamps.append(now)

    while trade_timestamps and now - trade_timestamps[0] > get_config("TRADE_WINDOW"):
        trade_timestamps.popleft()

    if len(trade_timestamps) > get_config("TRADE_LIMIT"):
        window = get_config("TRADE_WINDOW")
        msg = f"Trade rate exceeded: {len(trade_timestamps)} trades in {window} seconds."

        logging.warning(msg)
        send_email_alert("⚠️ Trade Rate Triggered", msg)
        trade_timestamps.clear()
        trade_state["in_trade_cooldown"] = True
        await asyncio.sleep(get_config("TRADE_COOLDOWN"))
        trade_state["in_trade_cooldown"] = False
        return False

    return True


def log_trade_to_csv(symbol, side, price, timestamp):
    filename = f"{symbol}_trades.csv"
    file_exists = os.path.isfile(filename)

    with open(filename, mode="a", newline="") as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(["timestamp", "side", "price"])
        writer.writerow([timestamp, side, price])


def log_trade_to_history(symbol, action, price, quantity=None, order_id=None, timestamp=None):
    """
    Append a real execution event to the shared trade_history.csv file.
    Multi-symbol safe.
    """
    path = app_state["paths"]["TRADE_HISTORY_FILE"]

    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with open(path, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            timestamp,
            symbol,
            action,
            f"{float(price):.2f}" if price is not None else "",
            str(quantity) if quantity is not None else "",
            str(order_id) if order_id is not None else "",
        ])
        

def log_trade_to_summary(symbol, buy_price, buy_time, sell_price, sell_time, trade_type="Standard"):
    profit_loss = float(sell_price) - float(buy_price)
    holding_time = (sell_time - buy_time).total_seconds()

    with open(app_state["paths"]["TRADE_SUMMARY_FILE"], mode="a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            symbol,
            f"{float(buy_price):.2f}",
            buy_time.strftime("%Y-%m-%d %H:%M:%S"),
            f"{float(sell_price):.2f}",
            sell_time.strftime("%Y-%m-%d %H:%M:%S"),
            f"{profit_loss:.2f}",
            f"{holding_time:.2f}",
            trade_type,
        ])

def handle_trade_update(now: float, price: float | None = None, symbol: str | None = None, volume: float = 0.0) -> None:
    """
    Backwards-compatible shim. Writes tick data to the MarketDataBuffer.
    Prefer calling app_state['market_data']['buffer'].update_tick(...) directly.
    """
    if now is None:
        now = time.time()
    if price is None:
        return
    if not symbol:
        # If caller didn't pass symbol, don't silently corrupt data.
        # This forces correct usage in multi-symbol mode.
        logging.warning("[handle_trade_update] symbol not provided; tick not stored.")
        return

    buf = app_state["market_data"]["buffer"]
    buf.update_tick(symbol=symbol, price=float(price), volume=float(volume), timestamp=float(now))