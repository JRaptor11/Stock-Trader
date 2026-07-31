import asyncio
from datetime import datetime, timezone
import time
import logging

from core.state import app_state, app_state_lock, fail_safe_event
from integrations.alerts import send_email_alert
from trading.trade_utils import log_trade_to_csv
from config.runtime_config import get_config
from safety.fail_safe_lifecycle import (
    mark_worker_awakened,
    queue_liquidations,
    reconcile,
)


# === Fail-Safe Actions ===

async def send_fail_safe_alert_async(subject: str, body: str) -> None:
    try:
        await asyncio.to_thread(send_email_alert, subject, body)
    except Exception:
        logging.warning("[FailSafe] Failed to send email alert; continuing.", exc_info=True)


def _normalize_symbol(symbol) -> str:
    return str(symbol or "").upper().strip()


def _is_held_trade_status(status: str | None) -> bool:
    status = str(status or "").lower().strip()
    return status in {"filled", "pending_sell", "synced"}


def _coerce_symbol_set(value) -> set[str]:
    if value is None:
        return set()

    if isinstance(value, str):
        return {_normalize_symbol(value)} if _normalize_symbol(value) else set()

    try:
        return {
            _normalize_symbol(symbol)
            for symbol in value
            if _normalize_symbol(symbol)
        }
    except TypeError:
        symbol = _normalize_symbol(value)
        return {symbol} if symbol else set()


def _pending_liquidation_set() -> set[str]:
    fs = app_state.setdefault("fail_safes", {})
    pending = fs.setdefault("pending_liquidation_symbols", [])
    return _coerce_symbol_set(pending)


def _layered_execution_enabled() -> bool:
    """
    True when the current architecture should let Layer 5 execute fail-safe sells.

    The current project still uses layer4_execution_enabled as the main env/config flag
    for the Layer 4 shadow -> Layer 5 execution path, so that flag is included here.
    """
    execution = app_state.get("execution", {}) or {}

    return bool(
        execution.get("layer5_execution_enabled")
        or execution.get("layer4_execution_enabled")
        or execution.get("layer3_execution_enabled")
    )


def _queue_fail_safe_liquidation(
    symbols,
    *,
    reason: str,
    liquidate_all: bool = False,
    trigger_price: float | None = None,
    entry_price: float | None = None,
    observed_loss_percent: float | None = None,
    position_qty: float | None = None,
) -> list[str]:
    return queue_liquidations(
        symbols,
        reason=reason,
        scope="global" if liquidate_all else "per_stock",
        trigger_price=trigger_price,
        entry_price=entry_price,
        observed_loss_percent=observed_loss_percent,
        position_qty=position_qty,
    )

async def sell_position(symbol, price=None):
    """
    Forces a sell of the given symbol if a position exists.

    Legacy behavior:
        fail_safes.py -> stream_manager._execute_sell(...)

    Current layered behavior:
        fail_safes.py -> queue liquidation request -> Layer 5 submits SELL

    This preserves the old stream fallback, but when layered execution is enabled,
    it does not try to sell through the legacy stream manager.
    """
    symbol = _normalize_symbol(symbol)
    liquidation_in_progress = app_state["fail_safes"].setdefault(
        "liquidation_in_progress",
        set(),
    )

    if app_state["stream"]["shutdown_event"].is_set():
        logging.warning(f"[SELL_POSITION] Shutdown active; refusing forced sell for {symbol}")
        return

    if symbol in liquidation_in_progress:
        logging.warning(f"[SELL_POSITION] Forced liquidation already in progress for {symbol}")
        return

    async with app_state["fail_safes"]["position_lock"]:
        trade_info = app_state.get("open_trades", {}).get(symbol)
        trade_status = (
            str(trade_info.get("status", "")).lower().strip()
            if isinstance(trade_info, dict)
            else ""
        )

        if not isinstance(trade_info, dict) or not _is_held_trade_status(trade_status):
            logging.warning(
                f"[SELL_POSITION] No active held trade found for {symbol}. "
                f"status={trade_status!r}. Aborting sell."
            )
            liquidation_in_progress.discard(symbol)
            return

        if _layered_execution_enabled():
            queued = _queue_fail_safe_liquidation(
                [symbol],
                reason=app_state.get("fail_safes", {}).get("last_trigger_reason") or "manual",
                liquidate_all=False,
            )
            logging.warning(
                "[SELL_POSITION] Layered execution enabled; queued forced liquidation "
                "for Layer 5 instead of using legacy stream sell | symbol=%s queued=%s",
                symbol,
                queued,
            )
            return

        stream_manager = app_state["stream"].get("manager")
        if not stream_manager:
            _queue_fail_safe_liquidation(
                [symbol],
                reason="legacy_sell_no_stream_manager",
                liquidate_all=False,
            )
            logging.error(
                "[SELL_POSITION] Stream manager not available. "
                "Queued forced sell for Layer 5 instead. symbol=%s",
                symbol,
            )
            return

        liquidation_in_progress.add(symbol)

        try:
            live_price = app_state.get("last_trade_price_by_symbol", {}).get(symbol)
            if live_price and live_price > 0:
                price = live_price

            if not price or price <= 0:
                price = trade_info.get("buy_price", 0)

            if not price or price <= 0:
                logging.error(f"[SELL_POSITION] No valid liquidation price available for {symbol}")
                return

            logging.info(f"[SELL_POSITION] 🚨 Forced liquidation for {symbol} at ${price:.2f}")

            app_state["strategy"].setdefault("last_sell", {})[symbol] = {
                "price": price,
                "time": datetime.now(timezone.utc),
                "entry_price": trade_info.get("buy_price", price),
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
    Triggers forced liquidation of all open positions if account equity drops below threshold.

    In layered mode, this queues liquidation requests for Layer 5.
    In legacy mode, this falls back to direct stream-manager sells.
    """
    tracker = app_state["services"].get("balance_tracker", {}).get("instance")
    if not tracker:
        logging.warning("⚠️ No balance tracker available for fail-safe check.")
        return

    info = tracker.get_balance()
    equity = info.get("equity", 0)
    equity_threshold = get_config("EQUITY_THRESHOLD")

    logging.debug(
        f"[FailSafe] Checking equity: ${equity:.2f} vs threshold: ${equity_threshold:.2f}"
    )

    if equity >= equity_threshold:
        return

    fs = app_state.setdefault("fail_safes", {})

    if fs.get("state") and fs.get("liquidate_all"):
        logging.debug("[FailSafe] Global fail-safe already active.")
        return

    logging.warning(f"⚠️ Global fail-safe triggered! Equity below {equity_threshold}")

    open_trades_snapshot = dict(app_state.get("open_trades", {}))
    held_symbols = []

    for symbol, trade_data in open_trades_snapshot.items():
        status = (
            str(trade_data.get("status", "")).lower().strip()
            if isinstance(trade_data, dict)
            else ""
        )

        if _is_held_trade_status(status):
            held_symbols.append(symbol)
        else:
            logging.debug(
                f"[FailSafe] Skipping global forced sell for {symbol}; local status={status!r}"
            )

    _queue_fail_safe_liquidation(
        held_symbols,
        reason="global",
        liquidate_all=True,
    )

    fail_safe_event.set()
    logging.error(f"[FailSafe] global state=True set at {time.strftime('%H:%M:%S')}")

    if _layered_execution_enabled():
        logging.warning(
            "[FailSafe] Global fail-safe worker signaled | symbols=%s",
            sorted(_coerce_symbol_set(held_symbols)),
        )
    else:
        for symbol, trade_data in open_trades_snapshot.items():
            status = (
                str(trade_data.get("status", "")).lower().strip()
                if isinstance(trade_data, dict)
                else ""
            )

            if not _is_held_trade_status(status):
                continue

            price = app_state.get("last_trade_price_by_symbol", {}).get(symbol)
            if not price or price <= 0:
                price = trade_data.get("buy_price", 0)

            await sell_position(symbol, price)

    if not app_state["fail_safes"].get("email_suppressed", False):
        await send_fail_safe_alert_async(
            "Global Fail-Safe Triggered",
            f"Equity dropped to ${equity:.2f}. Forced liquidation was triggered.",
        )


async def check_per_stock_fail_safe():
    with app_state_lock:
        fs = app_state.setdefault("fail_safes", {})
        cache = fs.setdefault("invalid_price_cache", {})
        fs.setdefault("state", False)
        fs.setdefault("pending_liquidation_symbols", [])
        fs.setdefault("liquidate_all", False)
        fs.setdefault("symbols", set())

        last_price_snapshot = dict(app_state.get("last_trade_price_by_symbol", {}))

    client = app_state.get("trading_client")
    if client is None:
        logging.warning(
            "[FailSafe] Skipping per-stock checks because no trading client is available."
        )
        return

    try:
        broker_positions = await asyncio.to_thread(client.get_all_positions)
    except Exception:
        logging.warning(
            "[FailSafe] Skipping per-stock checks because the broker position "
            "snapshot failed.",
            exc_info=True,
        )
        return

    for position in broker_positions or []:
        symbol = _normalize_symbol(getattr(position, "symbol", ""))
        if not symbol:
            continue

        try:
            quantity = float(getattr(position, "qty", 0) or 0)
            entry_price = float(getattr(position, "avg_entry_price", 0) or 0)
        except (TypeError, ValueError):
            logging.warning(
                "[FailSafe] Skipping %s because the broker position fields are invalid.",
                symbol,
            )
            continue

        if quantity <= 0:
            continue

        current_price = last_price_snapshot.get(symbol)

        logging.debug(
            "[FailSafe] %s: broker_qty=%s, broker_avg_entry=$%s, Current=$%s",
            symbol,
            quantity,
            entry_price,
            current_price,
        )

        with app_state_lock:
            last_cached = cache.get(symbol)

        if entry_price <= 0:
            with app_state_lock:
                if cache.get(symbol) == "invalid_entry":
                    continue
                cache[symbol] = "invalid_entry"

            logging.error(
                "[FailSafe] Invalid broker avg_entry_price (%s) for %s",
                entry_price,
                symbol,
            )
            continue

        if current_price is None or float(current_price) <= 0:
            with app_state_lock:
                if cache.get(symbol) == current_price:
                    continue
                cache[symbol] = current_price

            logging.warning(
                f"[FailSafe] Skipping per-stock check for {symbol} because no valid live price is available yet "
                f"(current_price={current_price})"
            )
            continue

        if last_cached is not None:
            with app_state_lock:
                cache.pop(symbol, None)

        current_price = float(current_price)
        percent_loss = ((entry_price - current_price) / entry_price) * 100
        threshold = get_config("MAX_POSITION_LOSS_PERCENT")

        logging.debug(
            f"[FailSafe] {symbol} loss: {percent_loss:.2f}% (Threshold: {threshold}%)"
        )

        if percent_loss < threshold:
            continue

        already_queued = symbol in _pending_liquidation_set()
        if already_queued:
            continue

        logging.warning(f"⚠️ Fail-safe triggered for {symbol}! Loss: {percent_loss:.2f}%")
        log_trade_to_csv(
            symbol,
            "FAILSAFE",
            current_price,
            time.strftime("%Y-%m-%d %H:%M:%S"),
        )

        _queue_fail_safe_liquidation(
            [symbol],
            reason="per_stock",
            liquidate_all=False,
            trigger_price=current_price,
            entry_price=entry_price,
            observed_loss_percent=percent_loss,
            position_qty=quantity,
        )

        fail_safe_event.set()
        logging.error(f"[FailSafe] state=True set at {time.strftime('%H:%M:%S')}")

        if _layered_execution_enabled():
            logging.warning(
                "[FailSafe] Per-stock fail-safe worker signaled | "
                "symbol=%s loss=%.2f%%",
                symbol,
                percent_loss,
            )
        else:
            await sell_position(symbol, current_price)

        if not already_queued:
            await send_fail_safe_alert_async(
                "Per-Stock Fail-Safe Triggered",
                f"{symbol} dropped {percent_loss:.2f}%. Forced sell was triggered.",
            )


async def fail_safe_liquidation_worker() -> None:
    """Immediate fail-safe path independent of the strategic REST-bar gate."""
    shutdown_event = app_state["stream"].get("shutdown_event")
    last_reconcile_at = 0.0

    while not shutdown_event.is_set():
        event_was_set = fail_safe_event.is_set()
        periodic_due = (
            app_state.get("fail_safes", {}).get("state")
            and time.monotonic() - last_reconcile_at >= 10.0
        )
        if not event_was_set and not periodic_due:
            await _sleep_with_shutdown(0.25)
            continue

        fail_safe_event.clear()
        mark_worker_awakened()
        logging.warning(
            "[FailSafeLifecycle] execution_worker_awakened_at=%s event_set=%s",
            datetime.now(timezone.utc).isoformat(),
            event_was_set,
        )

        try:
            await asyncio.to_thread(reconcile)
            last_reconcile_at = time.monotonic()
            if app_state.get("fail_safes", {}).get("state"):
                from layers.layer5_executor import execute_layer5_plan

                stamp = int(time.time() * 1000)
                await asyncio.to_thread(
                    execute_layer5_plan,
                    [],
                    {
                        "cycle_id": f"failsafe-{stamp}",
                        "plan_id": f"failsafe-{stamp}",
                    },
                )
        except Exception:
            logging.exception("[FailSafeLifecycle] Worker pass failed.")
            await _sleep_with_shutdown(1)


async def _sleep_with_shutdown(seconds: float, step: float = 0.25) -> None:
    """
    Sleep in small chunks so shutdown_event can interrupt quickly.
    Uses app_state["stream"]["shutdown_event"].
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
            logging.error(f"Error in fail-safe monitor: {e}", exc_info=True)
            await _sleep_with_shutdown(10)

    logging.info("[FailSafe] monitor_fail_safes exiting due to shutdown_event.")
