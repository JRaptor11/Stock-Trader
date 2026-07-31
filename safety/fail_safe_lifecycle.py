"""Broker-truth lifecycle state for forced liquidations.

This module deliberately contains no order-submission code. Layer 5 remains the
single broker submission path; this module owns queueing, deduplication,
terminal updates, retry eligibility, and cleanup.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import time
from typing import Any, Iterable
from uuid import uuid4

from core.state import app_state, app_state_lock, fail_safe_event
from config.runtime_config import get_config


FAIL_SAFE_RETRY_COOLDOWN_SECONDS = 30.0
ACTIVE_ORDER_STATUSES = {
    "new",
    "accepted",
    "pending_new",
    "accepted_for_bidding",
    "partially_filled",
    "held",
    "calculated",
}
TERMINAL_FAILURE_STATUSES = {
    "canceled",
    "expired",
    "rejected",
    "done_for_day",
    "stopped",
    "suspended",
}


def normalize_symbol(value: Any) -> str:
    return str(value or "").upper().strip()


def normalize_status(value: Any) -> str:
    return str(getattr(value, "value", value) or "").lower().strip()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _symbol_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        value = [value]
    try:
        return {normalize_symbol(item) for item in value if normalize_symbol(item)}
    except TypeError:
        symbol = normalize_symbol(value)
        return {symbol} if symbol else set()


def ensure_fail_safe_state() -> dict:
    fs = app_state.setdefault("fail_safes", {})
    fs.setdefault("state", False)
    fs.setdefault("global_active", False)
    fs.setdefault("liquidate_all", False)
    fs.setdefault("lifecycles", {})
    fs.setdefault("symbols", set())
    fs.setdefault("pending_liquidation_symbols", [])
    fs.setdefault("symbol", None)
    fs.setdefault("last_trigger_reason", None)
    fs.setdefault("reentry_block_until", {})
    return fs


def _audit(event: str, lifecycle: dict, *, old_state: str | None = None, error=None, details=None) -> None:
    try:
        from diagnostics.fail_safe_audit import append_fail_safe_transition
        append_fail_safe_transition(
            event,
            dict(lifecycle),
            old_state=old_state,
            error=error,
            details=details,
        )
    except Exception:
        logging.warning("[FailSafeLifecycle] Failed writing audit transition.", exc_info=True)


def queue_liquidations(
    symbols: Iterable[str],
    *,
    reason: str,
    scope: str,
    trigger_price: float | None = None,
    entry_price: float | None = None,
    observed_loss_percent: float | None = None,
    position_qty: float | None = None,
) -> list[str]:
    """Create each active lifecycle once and preserve its original trigger."""
    normalized = sorted({normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)})
    now = utc_now_iso()
    created: list[str] = []

    with app_state_lock:
        fs = ensure_fail_safe_state()
        lifecycles = fs["lifecycles"]
        active_symbols = _symbol_set(fs.get("symbols"))

        for symbol in normalized:
            lifecycle = lifecycles.get(symbol)
            if lifecycle and lifecycle.get("lifecycle_state") != "cleared":
                if scope == "global":
                    lifecycle["scope"] = "global"
                lifecycle.update(
                    {
                        "last_observed_at": now,
                        "trigger_price_latest": trigger_price,
                        "observed_loss_percent_latest": observed_loss_percent,
                    }
                )
                active_symbols.add(symbol)
                continue

            lifecycles[symbol] = {
                "symbol": symbol,
                "lifecycle_id": uuid4().hex,
                "scope": scope,
                "trigger_reason": reason,
                "triggered_at": now,
                "queued_at": now,
                "worker_awakened_at": None,
                "broker_snapshot_at": None,
                "trigger_price": trigger_price,
                "trigger_price_latest": trigger_price,
                "entry_price": entry_price,
                "observed_loss_percent": observed_loss_percent,
                "observed_loss_percent_latest": observed_loss_percent,
                "position_qty": position_qty,
                "lifecycle_state": "queued",
                "submission_attempt_at": None,
                "broker_accepted_at": None,
                "first_fill_at": None,
                "terminal_at": None,
                "position_zero_confirmed_at": None,
                "order_id": None,
                "order_status": None,
                "filled_qty": 0.0,
                "remaining_broker_position_qty": None,
                "retry_count": 0,
                "next_retry_at_epoch": 0.0,
                "last_error": None,
                "cleared_at": None,
                "entry_price_source": "alpaca_position_avg_entry_price",
            }
            active_symbols.add(symbol)
            created.append(symbol)

        fs["symbols"] = active_symbols
        fs["pending_liquidation_symbols"] = sorted(active_symbols)
        if scope == "global":
            fs["global_active"] = True
            fs["liquidate_all"] = True
        fs["state"] = bool(active_symbols or fs.get("global_active"))
        fs["last_trigger_reason"] = reason
        fs["updated_at"] = time.time()
        if len(normalized) == 1:
            fs["symbol"] = normalized[0]

    if created:
        for symbol in created:
            _audit("queued", ensure_fail_safe_state()["lifecycles"][symbol])
        fail_safe_event.set()
        logging.warning(
            "[FailSafeLifecycle] transition=queued scope=%s reason=%s symbols=%s "
            "queued_at=%s",
            scope,
            reason,
            created,
            now,
        )
    return created


def snapshot() -> dict:
    with app_state_lock:
        fs = ensure_fail_safe_state()
        now_epoch = time.time()
        reentry = fs.setdefault("reentry_block_until", {})
        expired = [
            symbol for symbol, until in reentry.items()
            if float(until or 0) <= now_epoch
        ]
        for symbol in expired:
            reentry.pop(symbol, None)
        lifecycles = {
            symbol: dict(data)
            for symbol, data in fs["lifecycles"].items()
            if isinstance(data, dict)
        }
        active_symbols = sorted(
            symbol
            for symbol, lifecycle in lifecycles.items()
            if lifecycle.get("lifecycle_state") != "cleared"
        )
        return {
            "event_set": fail_safe_event.is_set(),
            "state": bool(fs.get("state")),
            "active": bool(active_symbols or fs.get("global_active")),
            "global_active": bool(fs.get("global_active")),
            "liquidate_all": bool(fs.get("global_active")),
            "last_trigger_reason": fs.get("last_trigger_reason"),
            "symbol": normalize_symbol(fs.get("symbol")),
            "symbols": active_symbols,
            "pending_liquidation_symbols": active_symbols,
            "lifecycles": lifecycles,
            "reentry_block_until": dict(reentry),
            "reentry_blocked_symbols": sorted(reentry),
            "updated_at": fs.get("updated_at"),
        }


def should_block_buy(symbol: str) -> bool:
    state = snapshot()
    symbol = normalize_symbol(symbol)
    return (
        state["global_active"]
        or symbol in set(state["symbols"])
        or symbol in set(state["reentry_blocked_symbols"])
    )


def eligible_for_submission(symbol: str, *, now_epoch: float | None = None) -> bool:
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    symbol = normalize_symbol(symbol)
    with app_state_lock:
        lifecycle = ensure_fail_safe_state()["lifecycles"].get(symbol)
        if not lifecycle or lifecycle.get("lifecycle_state") == "cleared":
            return False
        state = lifecycle.get("lifecycle_state")
        if state not in {"queued", "waiting_retry", "failed"}:
            return False
        return now_epoch >= float(lifecycle.get("next_retry_at_epoch") or 0.0)


def mark_worker_awakened() -> None:
    now = utc_now_iso()
    with app_state_lock:
        for lifecycle in ensure_fail_safe_state()["lifecycles"].values():
            if lifecycle.get("lifecycle_state") != "cleared":
                lifecycle["worker_awakened_at"] = lifecycle.get("worker_awakened_at") or now


def mark_submission_started(symbol: str) -> bool:
    """Atomically claim a symbol for one Layer 5 submission attempt."""
    symbol = normalize_symbol(symbol)
    with app_state_lock:
        fs = ensure_fail_safe_state()
        lifecycle = fs["lifecycles"].get(symbol)
        if not lifecycle or not eligible_for_submission(symbol):
            return False
        old_state = lifecycle.get("lifecycle_state")
        lifecycle["lifecycle_state"] = "submitting"
        lifecycle["submission_attempt_at"] = utc_now_iso()
        lifecycle["retry_count"] = int(lifecycle.get("retry_count") or 0) + 1
        lifecycle["last_error"] = None
        audit_row = dict(lifecycle)
    _audit("submission_started", audit_row, old_state=old_state)
    return True


def mark_submitted(symbol: str, order: Any) -> None:
    symbol = normalize_symbol(symbol)
    now = utc_now_iso()
    with app_state_lock:
        lifecycle = ensure_fail_safe_state()["lifecycles"].setdefault(symbol, {"symbol": symbol})
        old_state = lifecycle.get("lifecycle_state")
        lifecycle.update(
            {
                "lifecycle_state": "submitted_open",
                "order_id": str(getattr(order, "id", "") or ""),
                "order_status": normalize_status(getattr(order, "status", "accepted")) or "accepted",
                "broker_accepted_at": now,
                "last_error": None,
            }
        )
        audit_row = dict(lifecycle)
    _audit("submitted", audit_row, old_state=old_state)
    logging.warning(
        "[FailSafeLifecycle] transition=submitted_open symbol=%s order_id=%s accepted_at=%s",
        symbol,
        getattr(order, "id", None),
        now,
    )


def mark_submission_failed(
    symbol: str,
    error: Any,
    *,
    cooldown_seconds: float = FAIL_SAFE_RETRY_COOLDOWN_SECONDS,
) -> None:
    symbol = normalize_symbol(symbol)
    now = utc_now_iso()
    with app_state_lock:
        lifecycle = ensure_fail_safe_state()["lifecycles"].get(symbol)
        if not lifecycle:
            return
        old_state = lifecycle.get("lifecycle_state")
        lifecycle.update(
            {
                "lifecycle_state": "waiting_retry",
                "order_status": "submission_failed",
                "terminal_at": now,
                "last_error": str(error),
                "next_retry_at_epoch": time.time() + max(0.0, float(cooldown_seconds)),
            }
        )
        audit_row = dict(lifecycle)
    _audit("submission_failed", audit_row, old_state=old_state, error=error)
    fail_safe_event.set()
    logging.error(
        "[FailSafeLifecycle] transition=waiting_retry symbol=%s retry_count=%s "
        "cooldown_seconds=%s error=%s",
        symbol,
        lifecycle.get("retry_count"),
        cooldown_seconds,
        error,
    )


def record_order_update(
    symbol: str,
    order: Any,
    status: str | None = None,
    *,
    lifecycle_id: str | None = None,
) -> None:
    symbol = normalize_symbol(symbol)
    incoming_order_id = str(getattr(order, "id", "") or "")
    status = normalize_status(status if status is not None else getattr(order, "status", ""))
    filled_qty = float(getattr(order, "filled_qty", 0) or 0)
    now = utc_now_iso()
    with app_state_lock:
        lifecycle = ensure_fail_safe_state()["lifecycles"].get(symbol)
        if not lifecycle:
            return
        lifecycle_order_id = str(lifecycle.get("order_id") or "")
        current_lifecycle_id = str(lifecycle.get("lifecycle_id") or "")
        if (
            not lifecycle_order_id
            or not incoming_order_id
            or incoming_order_id != lifecycle_order_id
            or (
                lifecycle_id is not None
                and str(lifecycle_id) != current_lifecycle_id
            )
        ):
            logging.debug(
                "[FailSafeLifecycle] Ignoring unrelated order update symbol=%s "
                "lifecycle_order_id=%s incoming_order_id=%s "
                "lifecycle_id=%s incoming_lifecycle_id=%s status=%s",
                symbol,
                lifecycle_order_id or None,
                incoming_order_id or None,
                current_lifecycle_id or None,
                lifecycle_id,
                status,
            )
            return
        old_state = lifecycle.get("lifecycle_state")
        lifecycle["order_status"] = status
        lifecycle["filled_qty"] = filled_qty
        if filled_qty > 0 and not lifecycle.get("first_fill_at"):
            lifecycle["first_fill_at"] = now
        if status == "partially_filled":
            lifecycle["lifecycle_state"] = "partially_filled"
        elif status == "filled":
            lifecycle["lifecycle_state"] = "filled_awaiting_position"
            lifecycle["terminal_at"] = now
        elif status in TERMINAL_FAILURE_STATUSES:
            lifecycle["lifecycle_state"] = "waiting_retry"
            lifecycle["terminal_at"] = now
            lifecycle["last_error"] = status
            lifecycle["next_retry_at_epoch"] = time.time() + FAIL_SAFE_RETRY_COOLDOWN_SECONDS
            fail_safe_event.set()
        audit_row = dict(lifecycle)
    if audit_row.get("lifecycle_state") != old_state or status in TERMINAL_FAILURE_STATUSES | {"filled"}:
        _audit("order_update", audit_row, old_state=old_state, details={"status": status})


def _position_quantities(positions: Iterable[Any]) -> dict[str, float]:
    quantities: dict[str, float] = {}
    for position in positions:
        symbol = normalize_symbol(getattr(position, "symbol", ""))
        if symbol:
            quantities[symbol] = max(0.0, float(getattr(position, "qty", 0) or 0))
    return quantities


def _open_sell_orders(open_orders: Iterable[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for order in open_orders:
        symbol = normalize_symbol(getattr(order, "symbol", ""))
        side = normalize_status(getattr(order, "side", ""))
        status = normalize_status(getattr(order, "status", ""))
        if symbol and side == "sell" and status not in TERMINAL_FAILURE_STATUSES | {"filled"}:
            result[symbol] = order
    return result


def reconcile(
    *,
    client: Any | None = None,
    positions: Iterable[Any] | None = None,
    open_orders: Iterable[Any] | None = None,
) -> dict:
    """Compare lifecycle state with broker positions and open sell orders."""
    client = client or app_state.get("trading_client")
    if positions is None:
        positions = client.get_all_positions() if client is not None else []
    if open_orders is None:
        if client is None:
            open_orders = []
        else:
            try:
                from alpaca.trading.enums import QueryOrderStatus
                from alpaca.trading.requests import GetOrdersRequest

                open_orders = client.get_orders(
                    filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
                )
            except ImportError:
                # Keeps the broker-truth coordinator independently unit-testable.
                open_orders = client.get_orders()

    quantities = _position_quantities(positions)
    open_sells = _open_sell_orders(open_orders)
    now = utc_now_iso()
    cleared: list[str] = []
    transitions: list[tuple[str, dict, str | None]] = []

    with app_state_lock:
        global_active = bool(ensure_fail_safe_state().get("global_active"))
        known_symbols = set(ensure_fail_safe_state()["lifecycles"])
    if global_active:
        newly_held = [
            symbol
            for symbol, quantity in quantities.items()
            if quantity > 0 and symbol not in known_symbols
        ]
        if newly_held:
            queue_liquidations(
                newly_held,
                reason="global_reconcile",
                scope="global",
            )

    with app_state_lock:
        fs = ensure_fail_safe_state()
        lifecycles = fs["lifecycles"]
        for symbol, lifecycle in lifecycles.items():
            if lifecycle.get("lifecycle_state") == "cleared":
                continue
            remaining = quantities.get(symbol, 0.0)
            lifecycle["broker_snapshot_at"] = now
            lifecycle["remaining_broker_position_qty"] = remaining
            order = open_sells.get(symbol)
            if order is not None:
                old_state = lifecycle.get("lifecycle_state")
                lifecycle["order_id"] = str(getattr(order, "id", "") or lifecycle.get("order_id") or "")
                lifecycle["order_status"] = normalize_status(getattr(order, "status", ""))
                lifecycle["filled_qty"] = float(getattr(order, "filled_qty", 0) or 0)
                lifecycle["lifecycle_state"] = (
                    "partially_filled" if lifecycle["filled_qty"] > 0 else "submitted_open"
                )
                if lifecycle["lifecycle_state"] != old_state:
                    transitions.append(("broker_open_order", dict(lifecycle), old_state))
            elif remaining <= 0:
                old_state = lifecycle.get("lifecycle_state")
                liquidation_was_attempted = bool(
                    lifecycle.get("order_id")
                    or int(lifecycle.get("retry_count") or 0) > 0
                    or float(lifecycle.get("filled_qty") or 0) > 0
                )
                cooldown_seconds = max(
                    0.0,
                    float(get_config("FAIL_SAFE_REENTRY_COOLDOWN_SECONDS") or 0),
                ) if liquidation_was_attempted else 0.0
                reentry_until = time.time() + cooldown_seconds
                lifecycle.update(
                    {
                        "lifecycle_state": "cleared",
                        "position_zero_confirmed_at": now,
                        "cleared_at": now,
                        "remaining_broker_position_qty": 0.0,
                        "reentry_block_until_epoch": reentry_until,
                    }
                )
                if cooldown_seconds > 0:
                    fs.setdefault("reentry_block_until", {})[symbol] = reentry_until
                cleared.append(symbol)
                transitions.append(("cleared", dict(lifecycle), old_state))
            elif lifecycle.get("lifecycle_state") in {
                "submitted_open",
                "partially_filled",
                "submitting",
                "filled_awaiting_position",
            }:
                old_state = lifecycle.get("lifecycle_state")
                lifecycle["lifecycle_state"] = "waiting_retry"
                lifecycle["next_retry_at_epoch"] = max(
                    float(lifecycle.get("next_retry_at_epoch") or 0.0),
                    time.time() + FAIL_SAFE_RETRY_COOLDOWN_SECONDS,
                )
                transitions.append(("retry_scheduled", dict(lifecycle), old_state))

        active = {
            symbol
            for symbol, lifecycle in lifecycles.items()
            if lifecycle.get("lifecycle_state") != "cleared"
        }
        fs["symbols"] = set(active)
        fs["pending_liquidation_symbols"] = sorted(active)
        if fs.get("symbol") not in active:
            fs["symbol"] = next(iter(sorted(active)), None)
        if fs.get("global_active"):
            global_active = any(
                lifecycle.get("scope") == "global"
                and lifecycle.get("lifecycle_state") != "cleared"
                for lifecycle in lifecycles.values()
            )
            fs["global_active"] = global_active
            fs["liquidate_all"] = global_active
        fs["state"] = bool(active or fs.get("global_active"))
        if not fs["state"]:
            fs["last_trigger_reason"] = None
            fail_safe_event.clear()

    for symbol in cleared:
        logging.warning(
            "[FailSafeLifecycle] transition=cleared symbol=%s broker_position_qty=0 "
            "open_sell_order_exists=false cleared_at=%s",
            symbol,
            now,
        )
    for event, lifecycle, old_state in transitions:
        _audit(event, lifecycle, old_state=old_state)
    return {
        "active_symbols": sorted(active),
        "cleared_symbols": cleared,
        "position_quantities": quantities,
        "open_sell_symbols": sorted(open_sells),
    }
