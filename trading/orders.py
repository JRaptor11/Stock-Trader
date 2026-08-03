import logging
import asyncio
import time
from datetime import datetime, timezone
from statistics import median
from threading import Lock
from alpaca.trading.enums import TimeInForce, QueryOrderStatus, OrderSide
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest
from utils.threading_utils import safe_thread
from trading.trade_utils import log_trade_to_summary
from layers.layer_csv import (
    append_layer_order_outcome_cycle_row,
    append_layer_order_outcome_row,
)
from core.state import app_state, app_state_lock
from safety.fail_safe_lifecycle import record_order_update, reconcile as reconcile_fail_safe


EXTENDED_LIMIT_ORDER_MAX_AGE_SECONDS = 15 * 60
CANCEL_EXTENDED_LIMITS_WHEN_MARKET_OPENS = True
_ORDER_OUTCOME_CYCLE_LOCK = Lock()


def _order_monitor_thread_entry() -> None:
    """
    Thread entrypoint: runs the async order monitor loop in its own event loop.
    Exits when shutdown_event is set OR open_orders is empty.
    """
    try:
        # Get coroutine from the monitor loop
        coro = monitor_open_orders_loop()

        # Defensive check: ensure we actually got a coroutine
        if not asyncio.iscoroutine(coro):
            raise TypeError(
                f"monitor_open_orders_loop must be async and return a coroutine, "
                f"but got {type(coro)!r}"
            )

        # Run the async loop inside this thread
        asyncio.run(coro)

    except asyncio.CancelledError:
        # Normal during shutdown
        logging.info("[OrderMonitor] cancelled during shutdown.")

    except Exception as e:
        logging.exception(f"[OrderMonitor] ❌ monitor thread crashed: {e}")

    finally:
        app_state["monitoring_orders"] = False
        logging.info("[OrderMonitor] ✅ Monitor thread exited.")

def normalize_side(side) -> str:
    return str(side).lower().replace("orderside.", "").strip()

def normalize_status(status) -> str:
    """
    Normalize Alpaca order status values so enum-style strings like
    'OrderStatus.FILLED' become plain 'filled'.
    """
    if status is None:
        return ""

    # If it's an enum object, prefer its .value
    value = getattr(status, "value", status)

    return str(value).lower().replace("orderstatus.", "").strip()

def set_entry_lock(symbol: str) -> None:
    symbol = str(symbol).upper().strip()
    app_state.setdefault("order_state", {}).setdefault("entry_locks", {})[symbol] = time.time()

def clear_entry_lock(symbol: str) -> None:
    symbol = str(symbol).upper().strip()
    app_state.setdefault("order_state", {}).setdefault("entry_locks", {}).pop(symbol, None)

def has_active_entry_lock(symbol: str) -> bool:
    symbol = str(symbol).upper().strip()
    order_state = app_state.setdefault("order_state", {})
    locks = order_state.setdefault("entry_locks", {})
    ts = locks.get(symbol)
    if ts is None:
        return False

    timeout = float(order_state.get("entry_lock_timeout_seconds", 90))
    if time.time() - ts > timeout:
        locks.pop(symbol, None)
        return False
    return True 

def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _parse_utc_datetime(
    value,
) -> datetime | None:
    if value is None:
        return None

    if isinstance(
        value,
        datetime,
    ):
        if value.tzinfo is None:
            return value.replace(
                tzinfo=timezone.utc
            )

        return value.astimezone(
            timezone.utc
        )

    try:
        parsed = datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00",
            )
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return parsed.astimezone(
            timezone.utc
        )

    except Exception:
        return None


def _iso_utc(
    value,
) -> str | None:
    parsed = _parse_utc_datetime(
        value
    )

    return (
        parsed.isoformat()
        if parsed is not None
        else None
    )


def _elapsed_seconds(
    start,
    end,
) -> float | None:
    start_dt = _parse_utc_datetime(
        start
    )

    end_dt = _parse_utc_datetime(
        end
    )

    if (
        start_dt is None
        or end_dt is None
    ):
        return None

    return round(
        max(
            0.0,
            (
                end_dt
                - start_dt
            ).total_seconds(),
        ),
        6,
    )


def _pct_delta(
    value,
    reference,
) -> float | None:
    value = _safe_float(
        value,
        0.0,
    )

    reference = _safe_float(
        reference,
        0.0,
    )

    if (
        value <= 0
        or reference <= 0
    ):
        return None

    return round(
        (
            value
            - reference
        )
        / reference,
        8,
    )


def _side_adjusted_slippage_pct(
    *,
    side,
    fill_price,
    reference_price,
) -> float | None:
    """
    Positive means adverse execution.

    BUY:
        fill above reference is adverse.

    SELL:
        fill below reference is adverse.
    """
    raw_delta = _pct_delta(
        fill_price,
        reference_price,
    )

    if raw_delta is None:
        return None

    normalized_side = normalize_side(
        side
    )

    if normalized_side == "buy":
        return raw_delta

    if normalized_side == "sell":
        return round(
            -raw_delta,
            8,
        )

    return None


def _terminal_order_timestamp(
    order,
    status: str,
    observed_at: datetime,
) -> datetime:
    status = normalize_status(
        status
    )

    status_field = {
        "filled": "filled_at",
        "canceled": "canceled_at",
        "expired": "expired_at",
        "rejected": "failed_at",
        "done_for_day": "updated_at",
    }.get(
        status
    )

    terminal_at = (
        _parse_utc_datetime(
            getattr(
                order,
                status_field,
                None,
            )
        )
        if status_field
        else None
    )

    if terminal_at is None:
        terminal_at = _parse_utc_datetime(
            getattr(
                order,
                "updated_at",
                None,
            )
        )

    return (
        terminal_at
        or observed_at
    )


def _terminal_order_message(
    order,
) -> str | None:
    for field in (
        "reject_reason",
        "failure_reason",
        "error",
        "message",
    ):
        value = getattr(
            order,
            field,
            None,
        )

        if value not in (
            None,
            "",
        ):
            return str(
                value
            )

    return None


def _build_terminal_order_outcome(
    *,
    symbol: str,
    tracked,
    order,
    status: str,
    observed_at: datetime | None = None,
) -> dict:
    """
    Join the 4A submission context with the broker's terminal
    order information.

    This function does not modify positions, orders, or execution.
    """
    observed_at = (
        observed_at
        or datetime.now(
            timezone.utc
        )
    )

    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(
            tzinfo=timezone.utc
        )
    else:
        observed_at = observed_at.astimezone(
            timezone.utc
        )

    if not isinstance(
        tracked,
        dict,
    ):
        tracked = {
            "order_id": tracked,
        }

    status = normalize_status(
        status
    )

    side = normalize_side(
        tracked.get("side")
        or getattr(
            order,
            "side",
            "",
        )
    )

    order_id = str(
        tracked.get("order_id")
        or getattr(
            order,
            "id",
            "",
        )
        or ""
    )

    submitted_at = (
        _parse_utc_datetime(
            getattr(
                order,
                "submitted_at",
                None,
            )
        )
        or _parse_utc_datetime(
            tracked.get(
                "broker_submitted_at"
            )
        )
        or _parse_utc_datetime(
            tracked.get(
                "broker_submit_completed_at"
            )
        )
        or _parse_utc_datetime(
            tracked.get(
                "tracked_at"
            )
        )
    )

    terminal_at = (
        _terminal_order_timestamp(
            order,
            status,
            observed_at,
        )
    )

    filled_at = _parse_utc_datetime(
        getattr(
            order,
            "filled_at",
            None,
        )
    )

    requested_qty = _safe_float(
        getattr(
            order,
            "qty",
            tracked.get(
                "qty",
                0.0,
            ),
        ),
        0.0,
    )

    filled_qty = _safe_float(
        getattr(
            order,
            "filled_qty",
            0.0,
        ),
        0.0,
    )

    filled_avg_price = _safe_float(
        getattr(
            order,
            "filled_avg_price",
            0.0,
        ),
        0.0,
    )

    unfilled_qty = max(
        0.0,
        requested_qty
        - filled_qty,
    )

    fill_ratio = (
        round(
            filled_qty
            / requested_qty,
            8,
        )
        if requested_qty > 0
        else None
    )

    filled_notional = (
        round(
            filled_qty
            * filled_avg_price,
            6,
        )
        if (
            filled_qty > 0
            and filled_avg_price > 0
        )
        else None
    )

    plan_price = _safe_float(
        tracked.get(
            "submission_plan_price"
        ),
        0.0,
    )

    reference_price = _safe_float(
        tracked.get(
            "submission_reference_price"
        ),
        0.0,
    )

    fill_vs_plan_pct = _pct_delta(
        filled_avg_price,
        plan_price,
    )

    fill_vs_reference_pct = _pct_delta(
        filled_avg_price,
        reference_price,
    )

    adverse_vs_plan = (
        _side_adjusted_slippage_pct(
            side=side,
            fill_price=filled_avg_price,
            reference_price=plan_price,
        )
    )

    adverse_vs_reference = (
        _side_adjusted_slippage_pct(
            side=side,
            fill_price=filled_avg_price,
            reference_price=reference_price,
        )
    )

    return {
        "timestamp": (
            observed_at.isoformat()
        ),

        "cycle_id": tracked.get(
            "cycle_id"
        ),
        "plan_id": tracked.get(
            "plan_id"
        ),
        "row_id": tracked.get(
            "row_id"
        ),

        "order_id": order_id,
        "client_order_id": getattr(
            order,
            "client_order_id",
            None,
        ),
        "symbol": symbol,
        "side": side,

        "terminal_status": status,
        "terminal_observed_at": (
            observed_at.isoformat()
        ),
        "terminal_at": (
            terminal_at.isoformat()
        ),

        "broker_submitted_at": (
            submitted_at.isoformat()
            if submitted_at is not None
            else None
        ),
        "broker_updated_at": _iso_utc(
            getattr(
                order,
                "updated_at",
                None,
            )
        ),
        "broker_filled_at": _iso_utc(
            getattr(
                order,
                "filled_at",
                None,
            )
        ),
        "broker_canceled_at": _iso_utc(
            getattr(
                order,
                "canceled_at",
                None,
            )
        ),
        "broker_expired_at": _iso_utc(
            getattr(
                order,
                "expired_at",
                None,
            )
        ),
        "broker_failed_at": _iso_utc(
            getattr(
                order,
                "failed_at",
                None,
            )
        ),

        "requested_qty": (
            requested_qty
        ),
        "filled_qty": filled_qty,
        "unfilled_qty": (
            round(
                unfilled_qty,
                8,
            )
        ),
        "fill_ratio": fill_ratio,

        "filled_avg_price": (
            filled_avg_price
            if filled_avg_price > 0
            else None
        ),
        "filled_notional": (
            filled_notional
        ),

        "submission_plan_price": (
            plan_price
            if plan_price > 0
            else None
        ),
        "submission_reference_price": (
            reference_price
            if reference_price > 0
            else None
        ),
        "submission_reference_source": (
            tracked.get(
                "submission_reference_source"
            )
        ),
        "submission_reference_tick_timestamp": (
            tracked.get(
                "submission_reference_tick_timestamp"
            )
        ),
        "submission_reference_age_seconds": (
            tracked.get(
                "submission_reference_age_seconds"
            )
        ),
        "submission_reference_vs_plan_pct": (
            tracked.get(
                "submission_reference_vs_plan_pct"
            )
        ),

        "fill_vs_plan_pct": (
            fill_vs_plan_pct
        ),
        "fill_vs_reference_pct": (
            fill_vs_reference_pct
        ),
        "adverse_slippage_vs_plan_pct": (
            adverse_vs_plan
        ),
        "adverse_slippage_vs_reference_pct": (
            adverse_vs_reference
        ),

        "submission_to_terminal_seconds": (
            _elapsed_seconds(
                submitted_at,
                terminal_at,
            )
        ),
        "time_to_fill_seconds": (
            _elapsed_seconds(
                submitted_at,
                filled_at,
            )
        ),
        "monitor_detection_delay_seconds": (
            _elapsed_seconds(
                terminal_at,
                observed_at,
            )
        ),

        "broker_submit_started_at": (
            tracked.get(
                "broker_submit_started_at"
            )
        ),
        "broker_submit_completed_at": (
            tracked.get(
                "broker_submit_completed_at"
            )
        ),
        "broker_submit_latency_ms": (
            tracked.get(
                "broker_submit_latency_ms"
            )
        ),
        "broker_status_at_submit": (
            tracked.get(
                "broker_status_at_submit"
            )
        ),
        "broker_created_at": (
            tracked.get(
                "broker_created_at"
            )
        ),
        "broker_limit_price": (
            tracked.get(
                "broker_limit_price"
            )
        ),

        "tracked_at": tracked.get(
            "tracked_at"
        ),
        "market_is_open": tracked.get(
            "market_is_open"
        ),
        "reason": tracked.get(
            "reason"
        ),
        "planned_notional": tracked.get(
            "planned_notional"
        ),

        "cancel_requested": bool(
            tracked.get(
                "cancel_requested"
            )
        ),
        "cancel_reason": tracked.get(
            "cancel_reason"
        ),
        "cancel_requested_at": tracked.get(
            "cancel_requested_at"
        ),

        "terminal_message": (
            _terminal_order_message(
                order
            )
        ),
    }


def _order_outcome_cycle_key(
    cycle_id,
    plan_id,
) -> str | None:
    plan_text = str(
        plan_id or ""
    ).strip()

    if plan_text:
        return f"plan:{plan_text}"

    if cycle_id not in (
        None,
        "",
    ):
        return f"cycle:{cycle_id}"

    return None


def _order_outcome_cycles_state() -> dict:
    return (
        app_state.setdefault(
            "layers",
            {},
        )
        .setdefault(
            "order_outcomes",
            {},
        )
        .setdefault(
            "cycles",
            {},
        )
    )


def _outcome_summary_number(
    value,
) -> float | None:
    if value in (
        None,
        "",
    ):
        return None

    try:
        number = float(
            value
        )
    except Exception:
        return None

    if number != number:
        return None

    if number in (
        float("inf"),
        float("-inf"),
    ):
        return None

    return number


def _outcome_summary_values(
    rows: list[dict],
    field: str,
) -> list[float]:
    values = []

    for row in rows or []:
        if not isinstance(
            row,
            dict,
        ):
            continue

        value = (
            _outcome_summary_number(
                row.get(field)
            )
        )

        if value is not None:
            values.append(
                value
            )

    return values


def _outcome_summary_median(
    rows: list[dict],
    field: str,
) -> float | None:
    values = (
        _outcome_summary_values(
            rows,
            field,
        )
    )

    if not values:
        return None

    return round(
        median(values),
        8,
    )


def _outcome_summary_max(
    rows: list[dict],
    field: str,
) -> float | None:
    values = (
        _outcome_summary_values(
            rows,
            field,
        )
    )

    if not values:
        return None

    return round(
        max(values),
        8,
    )


def _outcome_adverse_slippage_dollars(
    outcome: dict,
    *,
    reference_field: str,
) -> float | None:
    if not isinstance(
        outcome,
        dict,
    ):
        return None

    side = normalize_side(
        outcome.get("side")
    )

    fill_price = (
        _outcome_summary_number(
            outcome.get(
                "filled_avg_price"
            )
        )
    )

    reference_price = (
        _outcome_summary_number(
            outcome.get(
                reference_field
            )
        )
    )

    filled_qty = (
        _outcome_summary_number(
            outcome.get(
                "filled_qty"
            )
        )
    )

    if (
        fill_price is None
        or reference_price is None
        or filled_qty is None
        or fill_price <= 0
        or reference_price <= 0
        or filled_qty <= 0
    ):
        return None

    if side == "buy":
        dollars = (
            fill_price
            - reference_price
        ) * filled_qty

    elif side == "sell":
        dollars = (
            reference_price
            - fill_price
        ) * filled_qty

    else:
        return None

    return round(
        dollars,
        6,
    )


def _build_order_outcome_cycle_summary(
    cycle_state: dict,
    *,
    generated_at: datetime | None = None,
) -> dict:
    generated_at = (
        generated_at
        or datetime.now(
            timezone.utc
        )
    )

    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(
            tzinfo=timezone.utc
        )
    else:
        generated_at = generated_at.astimezone(
            timezone.utc
        )

    cycle_state = (
        cycle_state
        if isinstance(
            cycle_state,
            dict,
        )
        else {}
    )

    expected_order_ids = [
        str(order_id)
        for order_id in (
            cycle_state.get(
                "expected_order_ids"
            )
            or []
        )
        if str(
            order_id or ""
        ).strip()
    ]

    expected_order_ids = list(
        dict.fromkeys(
            expected_order_ids
        )
    )

    metadata_by_id = (
        cycle_state.get(
            "order_metadata_by_id"
        )
        or {}
    )

    outcomes_by_id = (
        cycle_state.get(
            "outcomes_by_order_id"
        )
        or {}
    )

    terminal_outcomes = [
        outcomes_by_id[order_id]
        for order_id in expected_order_ids
        if (
            order_id
            in outcomes_by_id
            and isinstance(
                outcomes_by_id[
                    order_id
                ],
                dict,
            )
        )
    ]

    expected_count = len(
        expected_order_ids
    )

    terminal_count = len(
        terminal_outcomes
    )

    cycle_complete = bool(
        expected_count > 0
        and terminal_count
        == expected_count
    )

    def expected_side(
        order_id: str,
    ) -> str:
        metadata = (
            metadata_by_id.get(
                order_id
            )
            or {}
        )

        return normalize_side(
            metadata.get("side")
        )

    expected_buy_count = sum(
        1
        for order_id
        in expected_order_ids
        if expected_side(
            order_id
        )
        == "buy"
    )

    expected_sell_count = sum(
        1
        for order_id
        in expected_order_ids
        if expected_side(
            order_id
        )
        == "sell"
    )

    terminal_status_counts = {}

    for outcome in terminal_outcomes:
        status = normalize_status(
            outcome.get(
                "terminal_status"
            )
        ) or "unknown"

        terminal_status_counts[
            status
        ] = (
            terminal_status_counts.get(
                status,
                0,
            )
            + 1
        )

    terminal_status_counts = dict(
        sorted(
            terminal_status_counts.items()
        )
    )

    filled_outcomes = [
        outcome
        for outcome in terminal_outcomes
        if normalize_status(
            outcome.get(
                "terminal_status"
            )
        )
        == "filled"
    ]

    nonfilled_outcomes = [
        outcome
        for outcome in terminal_outcomes
        if normalize_status(
            outcome.get(
                "terminal_status"
            )
        )
        != "filled"
    ]

    filled_buy_count = sum(
        1
        for outcome in filled_outcomes
        if normalize_side(
            outcome.get("side")
        )
        == "buy"
    )

    filled_sell_count = sum(
        1
        for outcome in filled_outcomes
        if normalize_side(
            outcome.get("side")
        )
        == "sell"
    )

    full_fill_count = sum(
        1
        for outcome in filled_outcomes
        if (
            _outcome_summary_number(
                outcome.get(
                    "fill_ratio"
                )
            )
            or 0.0
        )
        >= 0.999999
    )

    reference_slippage_rows = [
        outcome
        for outcome in filled_outcomes
        if _outcome_summary_number(
            outcome.get(
                "adverse_slippage_vs_reference_pct"
            )
        )
        is not None
    ]

    plan_slippage_rows = [
        outcome
        for outcome in filled_outcomes
        if _outcome_summary_number(
            outcome.get(
                "adverse_slippage_vs_plan_pct"
            )
        )
        is not None
    ]

    buy_reference_rows = [
        outcome
        for outcome in reference_slippage_rows
        if normalize_side(
            outcome.get("side")
        )
        == "buy"
    ]

    sell_reference_rows = [
        outcome
        for outcome in reference_slippage_rows
        if normalize_side(
            outcome.get("side")
        )
        == "sell"
    ]

    reference_dollar_values = [
        value
        for value in (
            _outcome_adverse_slippage_dollars(
                outcome,
                reference_field=(
                    "submission_reference_price"
                ),
            )
            for outcome in filled_outcomes
        )
        if value is not None
    ]

    plan_dollar_values = [
        value
        for value in (
            _outcome_adverse_slippage_dollars(
                outcome,
                reference_field=(
                    "submission_plan_price"
                ),
            )
            for outcome in filled_outcomes
        )
        if value is not None
    ]

    worst_reference_outcome = None
    worst_reference_pct = None

    for outcome in reference_slippage_rows:
        value = (
            _outcome_summary_number(
                outcome.get(
                    "adverse_slippage_vs_reference_pct"
                )
            )
        )

        if value is None:
            continue

        if (
            worst_reference_pct
            is None
            or value
            > worst_reference_pct
        ):
            worst_reference_pct = value
            worst_reference_outcome = (
                outcome
            )

    requested_qty_total = round(
        sum(
            _outcome_summary_number(
                outcome.get(
                    "requested_qty"
                )
            )
            or 0.0
            for outcome in terminal_outcomes
        ),
        8,
    )

    filled_qty_total = round(
        sum(
            _outcome_summary_number(
                outcome.get(
                    "filled_qty"
                )
            )
            or 0.0
            for outcome in terminal_outcomes
        ),
        8,
    )

    filled_notional_total = round(
        sum(
            _outcome_summary_number(
                outcome.get(
                    "filled_notional"
                )
            )
            or 0.0
            for outcome in filled_outcomes
        ),
        6,
    )

    terminal_symbols = sorted({
        str(
            outcome.get("symbol")
            or ""
        ).upper().strip()
        for outcome in terminal_outcomes
        if str(
            outcome.get("symbol")
            or ""
        ).strip()
    })

    filled_symbols = sorted({
        str(
            outcome.get("symbol")
            or ""
        ).upper().strip()
        for outcome in filled_outcomes
        if str(
            outcome.get("symbol")
            or ""
        ).strip()
    })

    nonfilled_symbols = sorted({
        str(
            outcome.get("symbol")
            or ""
        ).upper().strip()
        for outcome in nonfilled_outcomes
        if str(
            outcome.get("symbol")
            or ""
        ).strip()
    })

    reported_submitted_count = (
        cycle_state.get(
            "execution_reported_submitted_count"
        )
    )

    try:
        reported_submitted_count = int(
            reported_submitted_count
        )
    except Exception:
        reported_submitted_count = None

    return {
        "timestamp": (
            generated_at.isoformat()
        ),

        "cycle_id": cycle_state.get(
            "cycle_id"
        ),
        "plan_id": cycle_state.get(
            "plan_id"
        ),

        "execution_started_at": (
            cycle_state.get(
                "execution_started_at"
            )
        ),
        "execution_finished_at": (
            cycle_state.get(
                "execution_finished_at"
            )
        ),

        "execution_reported_submitted_count": (
            reported_submitted_count
        ),
        "expected_submitted_count": (
            expected_count
        ),
        "terminal_order_count": (
            terminal_count
        ),
        "cycle_complete": cycle_complete,

        "submitted_count_integrity_ok": (
            reported_submitted_count
            == expected_count
            if reported_submitted_count
            is not None
            else None
        ),

        "expected_buy_count": (
            expected_buy_count
        ),
        "expected_sell_count": (
            expected_sell_count
        ),

        "filled_order_count": len(
            filled_outcomes
        ),
        "nonfilled_terminal_count": len(
            nonfilled_outcomes
        ),

        "filled_buy_count": (
            filled_buy_count
        ),
        "filled_sell_count": (
            filled_sell_count
        ),
        "full_fill_count": (
            full_fill_count
        ),

        "fill_rate": (
            round(
                len(
                    filled_outcomes
                )
                / expected_count,
                8,
            )
            if expected_count
            else None
        ),

        "full_fill_rate": (
            round(
                full_fill_count
                / expected_count,
                8,
            )
            if expected_count
            else None
        ),

        "terminal_status_counts": (
            terminal_status_counts
        ),

        "requested_qty_total": (
            requested_qty_total
        ),
        "filled_qty_total": (
            filled_qty_total
        ),
        "filled_notional_total": (
            filled_notional_total
        ),

        "reference_slippage_sample_count": (
            len(
                reference_slippage_rows
            )
        ),
        "reference_slippage_coverage_rate": (
            round(
                len(
                    reference_slippage_rows
                )
                / len(
                    filled_outcomes
                ),
                8,
            )
            if filled_outcomes
            else None
        ),

        "plan_slippage_sample_count": (
            len(
                plan_slippage_rows
            )
        ),
        "plan_slippage_coverage_rate": (
            round(
                len(
                    plan_slippage_rows
                )
                / len(
                    filled_outcomes
                ),
                8,
            )
            if filled_outcomes
            else None
        ),

        "median_time_to_fill_seconds": (
            _outcome_summary_median(
                filled_outcomes,
                "time_to_fill_seconds",
            )
        ),
        "max_time_to_fill_seconds": (
            _outcome_summary_max(
                filled_outcomes,
                "time_to_fill_seconds",
            )
        ),

        "median_monitor_detection_delay_seconds": (
            _outcome_summary_median(
                terminal_outcomes,
                "monitor_detection_delay_seconds",
            )
        ),
        "max_monitor_detection_delay_seconds": (
            _outcome_summary_max(
                terminal_outcomes,
                "monitor_detection_delay_seconds",
            )
        ),

        "median_adverse_slippage_vs_reference_pct": (
            _outcome_summary_median(
                reference_slippage_rows,
                "adverse_slippage_vs_reference_pct",
            )
        ),
        "max_adverse_slippage_vs_reference_pct": (
            _outcome_summary_max(
                reference_slippage_rows,
                "adverse_slippage_vs_reference_pct",
            )
        ),

        "median_adverse_slippage_vs_plan_pct": (
            _outcome_summary_median(
                plan_slippage_rows,
                "adverse_slippage_vs_plan_pct",
            )
        ),
        "max_adverse_slippage_vs_plan_pct": (
            _outcome_summary_max(
                plan_slippage_rows,
                "adverse_slippage_vs_plan_pct",
            )
        ),

        "median_buy_adverse_slippage_vs_reference_pct": (
            _outcome_summary_median(
                buy_reference_rows,
                "adverse_slippage_vs_reference_pct",
            )
        ),
        "median_sell_adverse_slippage_vs_reference_pct": (
            _outcome_summary_median(
                sell_reference_rows,
                "adverse_slippage_vs_reference_pct",
            )
        ),

        "total_adverse_slippage_vs_reference_dollars": (
            round(
                sum(
                    reference_dollar_values
                ),
                6,
            )
            if reference_dollar_values
            else None
        ),
        "total_adverse_slippage_vs_plan_dollars": (
            round(
                sum(
                    plan_dollar_values
                ),
                6,
            )
            if plan_dollar_values
            else None
        ),

        "worst_reference_order_id": (
            worst_reference_outcome.get(
                "order_id"
            )
            if worst_reference_outcome
            else None
        ),
        "worst_reference_symbol": (
            worst_reference_outcome.get(
                "symbol"
            )
            if worst_reference_outcome
            else None
        ),
        "worst_reference_side": (
            worst_reference_outcome.get(
                "side"
            )
            if worst_reference_outcome
            else None
        ),
        "worst_reference_adverse_slippage_pct": (
            round(
                worst_reference_pct,
                8,
            )
            if worst_reference_pct
            is not None
            else None
        ),
        "worst_reference_adverse_slippage_dollars": (
            _outcome_adverse_slippage_dollars(
                worst_reference_outcome,
                reference_field=(
                    "submission_reference_price"
                ),
            )
            if worst_reference_outcome
            else None
        ),

        "terminal_symbols": (
            terminal_symbols
        ),
        "filled_symbols": filled_symbols,
        "nonfilled_symbols": (
            nonfilled_symbols
        ),
    }


def _maybe_append_order_outcome_cycle_summary(
    cycle_key: str | None,
) -> dict | None:
    if not cycle_key:
        return None

    with _ORDER_OUTCOME_CYCLE_LOCK:
        cycles = (
            _order_outcome_cycles_state()
        )

        cycle_state = cycles.get(
            cycle_key
        )

        if not isinstance(
            cycle_state,
            dict,
        ):
            return None

        if cycle_state.get(
            "summary_emitted"
        ):
            existing = cycle_state.get(
                "summary"
            )

            return (
                dict(existing)
                if isinstance(
                    existing,
                    dict,
                )
                else None
            )

        if cycle_state.get(
            "summary_pending"
        ):
            return None

        summary = (
            _build_order_outcome_cycle_summary(
                cycle_state
            )
        )

        if not summary.get(
            "cycle_complete"
        ):
            return None

        cycle_state[
            "summary_pending"
        ] = True

    try:
        append_layer_order_outcome_cycle_row(
            summary
        )

    except Exception:
        with _ORDER_OUTCOME_CYCLE_LOCK:
            cycle_state = (
                _order_outcome_cycles_state()
                .get(
                    cycle_key
                )
            )

            if isinstance(
                cycle_state,
                dict,
            ):
                cycle_state[
                    "summary_pending"
                ] = False

        logging.warning(
            "[OrderOutcomeCycle] Failed "
            "writing completed cycle summary | "
            "cycle_key=%s",
            cycle_key,
            exc_info=True,
        )

        return None

    with _ORDER_OUTCOME_CYCLE_LOCK:
        cycle_state = (
            _order_outcome_cycles_state()
            .get(
                cycle_key
            )
        )

        if isinstance(
            cycle_state,
            dict,
        ):
            cycle_state.update({
                "summary_pending": False,
                "summary_emitted": True,
                "summary": dict(
                    summary
                ),
                "summary_emitted_at": (
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                ),
            })

    logging.info(
        "[OrderOutcomeCycle] Complete | "
        "cycle_id=%s plan_id=%s "
        "orders=%s filled=%s "
        "fill_rate=%s median_fill_seconds=%s "
        "median_adverse_reference=%s "
        "worst_symbol=%s worst_adverse=%s",
        summary.get("cycle_id"),
        summary.get("plan_id"),
        summary.get(
            "expected_submitted_count"
        ),
        summary.get(
            "filled_order_count"
        ),
        summary.get("fill_rate"),
        summary.get(
            "median_time_to_fill_seconds"
        ),
        summary.get(
            "median_adverse_slippage_vs_reference_pct"
        ),
        summary.get(
            "worst_reference_symbol"
        ),
        summary.get(
            "worst_reference_adverse_slippage_pct"
        ),
    )

    return summary


def _store_terminal_order_outcome_for_cycle(
    outcome: dict,
) -> dict | None:
    if not isinstance(
        outcome,
        dict,
    ):
        return None

    cycle_key = (
        _order_outcome_cycle_key(
            outcome.get("cycle_id"),
            outcome.get("plan_id"),
        )
    )

    order_id = str(
        outcome.get("order_id")
        or ""
    ).strip()

    if (
        not cycle_key
        or not order_id
    ):
        return None

    with _ORDER_OUTCOME_CYCLE_LOCK:
        cycles = (
            _order_outcome_cycles_state()
        )

        cycle_state = cycles.setdefault(
            cycle_key,
            {
                "cycle_id": outcome.get(
                    "cycle_id"
                ),
                "plan_id": outcome.get(
                    "plan_id"
                ),
                "expected_order_ids": [],
                "order_metadata_by_id": {},
                "outcomes_by_order_id": {},
                "summary_pending": False,
                "summary_emitted": False,
            },
        )

        cycle_state[
            "outcomes_by_order_id"
        ][order_id] = dict(
            outcome
        )

    return (
        _maybe_append_order_outcome_cycle_summary(
            cycle_key
        )
    )


def register_layer5_cycle_submissions(
    result: dict | None,
) -> dict | None:
    """
    Register the authoritative order IDs after Layer 5 finishes
    its submission loop.

    Terminal outcomes may arrive before or after this call.
    """
    result = (
        result
        if isinstance(
            result,
            dict,
        )
        else {}
    )

    submitted_rows = [
        row
        for row in (
            result.get("orders")
            or []
        )
        if (
            isinstance(
                row,
                dict,
            )
            and normalize_status(
                row.get("status")
            )
            == "submitted"
            and str(
                row.get("order_id")
                or ""
            ).strip()
        )
    ]

    if not submitted_rows:
        return None

    cycle_id = result.get(
        "cycle_id"
    )

    plan_id = result.get(
        "plan_id"
    )

    cycle_key = (
        _order_outcome_cycle_key(
            cycle_id,
            plan_id,
        )
    )

    if not cycle_key:
        return None

    order_metadata = {}

    for row in submitted_rows:
        order_id = str(
            row.get("order_id")
        ).strip()

        order_metadata[
            order_id
        ] = {
            "order_id": order_id,
            "symbol": row.get(
                "symbol"
            ),
            "side": normalize_side(
                row.get("side")
            ),
            "row_id": row.get(
                "row_id"
            ),
        }

    expected_ids = list(
        order_metadata.keys()
    )

    with _ORDER_OUTCOME_CYCLE_LOCK:
        cycles = (
            _order_outcome_cycles_state()
        )

        cycle_state = cycles.setdefault(
            cycle_key,
            {
                "cycle_id": cycle_id,
                "plan_id": plan_id,
                "expected_order_ids": [],
                "order_metadata_by_id": {},
                "outcomes_by_order_id": {},
                "summary_pending": False,
                "summary_emitted": False,
            },
        )

        cycle_state.update({
            "cycle_id": cycle_id,
            "plan_id": plan_id,
            "execution_started_at": (
                result.get(
                    "started_at"
                )
            ),
            "execution_finished_at": (
                result.get(
                    "finished_at"
                )
            ),
            "execution_reported_submitted_count": (
                result.get(
                    "submitted"
                )
            ),
            "expected_order_ids": (
                expected_ids
            ),
        })

        cycle_state.setdefault(
            "order_metadata_by_id",
            {},
        ).update(
            order_metadata
        )

        cycle_state.setdefault(
            "outcomes_by_order_id",
            {},
        )

    return (
        _maybe_append_order_outcome_cycle_summary(
            cycle_key
        )
    )


def _record_terminal_order_outcome(
    *,
    symbol: str,
    tracked,
    order,
    status: str,
) -> dict | None:
    """
    Persist one terminal order outcome.

    Diagnostic failures must never prevent normal order finalization.
    """
    try:
        outcome = (
            _build_terminal_order_outcome(
                symbol=symbol,
                tracked=tracked,
                order=order,
                status=status,
            )
        )

        append_layer_order_outcome_row(
            outcome
        )

        outcome_state = (
            app_state.setdefault(
                "layers",
                {},
            ).setdefault(
                "order_outcomes",
                {},
            )
        )

        outcome_state[
            "last"
        ] = dict(
            outcome
        )

        recent = outcome_state.setdefault(
            "recent",
            [],
        )

        recent.append(
            dict(
                outcome
            )
        )

        del recent[:-200]

        _store_terminal_order_outcome_for_cycle(
            outcome
        )

        logging.info(
            "[OrderOutcome] Terminal order | "
            "symbol=%s side=%s status=%s "
            "order_id=%s cycle_id=%s "
            "filled_qty=%s fill_price=%s "
            "time_to_fill=%s "
            "adverse_slippage_reference=%s",
            symbol,
            outcome.get("side"),
            outcome.get(
                "terminal_status"
            ),
            outcome.get("order_id"),
            outcome.get("cycle_id"),
            outcome.get("filled_qty"),
            outcome.get(
                "filled_avg_price"
            ),
            outcome.get(
                "time_to_fill_seconds"
            ),
            outcome.get(
                "adverse_slippage_vs_reference_pct"
            ),
        )

        return outcome

    except Exception:
        logging.warning(
            "[OrderOutcome] Failed recording "
            "terminal outcome | symbol=%s "
            "status=%s",
            symbol,
            status,
            exc_info=True,
        )

        return None


def _get_open_trade_status(symbol: str) -> str | None:
    trade_info = app_state.get("open_trades", {}).get(symbol)
    if not isinstance(trade_info, dict):
        return None
    status = trade_info.get("status")
    return str(status).lower().strip() if status is not None else None


def _has_broker_pending_sell(symbol: str) -> bool:
    try:
        params = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        open_orders = app_state["trading_client"].get_orders(filter=params)
        return any(
            getattr(o, "symbol", None) == symbol
            and normalize_side(getattr(o, "side", "")) == "sell"
            for o in open_orders
        )
    except Exception as e:
        logging.warning(f"⚠️ Error checking open sell orders for {symbol}: {e}")
        return True  # safest fallback


def _sync_local_position_from_broker(
    symbol: str,
    *,
    source: str,
) -> tuple[bool, dict | None]:
    """
    Replace local state with the broker's aggregate position after an order.

    A terminal order reports only that order's filled quantity. It does not
    report the combined/remaining account position, so using it directly
    corrupts local state after scale-ins and partial sells.
    """
    client = app_state.get("trading_client")
    if client is None:
        return False, None

    try:
        positions = list(client.get_all_positions() or [])
    except Exception:
        logging.warning(
            "[PositionSync] Broker position refresh failed after %s for %s.",
            source,
            symbol,
            exc_info=True,
        )
        return False, None

    broker_position = next(
        (
            position
            for position in positions
            if str(getattr(position, "symbol", "") or "").upper().strip()
            == symbol
            and _safe_float(getattr(position, "qty", 0), 0) > 0
        ),
        None,
    )

    with app_state_lock:
        open_trades = app_state.setdefault("open_trades", {})
        previous = open_trades.get(symbol)

        if broker_position is None:
            open_trades.pop(symbol, None)
            return True, None

        qty = _safe_float(getattr(broker_position, "qty", 0), 0)
        avg_entry = _safe_float(
            getattr(broker_position, "avg_entry_price", 0),
            0,
        )
        now = datetime.now(timezone.utc)
        updated = dict(previous) if isinstance(previous, dict) else {}
        updated.update({
            "buy_price": avg_entry,
            "buy_time": updated.get("buy_time") or now,
            "quantity": qty,
            "status": "synced",
            "max_price": max(
                _safe_float(updated.get("max_price"), avg_entry),
                avg_entry,
            ),
            "reconciled_at": now.isoformat(),
            "reconcile_source": source,
        })
        for key in (
            "pending_buy_order_id",
            "pending_buy_quantity",
            "pending_sell_order_id",
            "sell_order_id",
            "sell_quantity",
            "broker_quantity_before_order",
        ):
            updated.pop(key, None)
        open_trades[symbol] = updated
        return True, dict(updated)


def _fallback_terminal_position_update(
    symbol: str,
    *,
    side: str,
    filled_qty: float,
    filled_price: float,
) -> dict | None:
    """Conservative fallback used only when the broker snapshot is unavailable."""
    with app_state_lock:
        open_trades = app_state.setdefault("open_trades", {})
        previous = open_trades.get(symbol)
        previous = dict(previous) if isinstance(previous, dict) else {}
        base_qty = _safe_float(
            previous.get(
                "broker_quantity_before_order",
                previous.get("quantity", 0),
            ),
            0,
        )
        remaining_qty = (
            base_qty + filled_qty
            if side == "buy"
            else max(0.0, base_qty - filled_qty)
        )

        if remaining_qty <= 0:
            open_trades.pop(symbol, None)
            return None

        previous.update({
            "quantity": remaining_qty,
            "status": "filled",
            "buy_price": _safe_float(
                previous.get("buy_price"),
                filled_price,
            ) or filled_price,
            "buy_time": previous.get("buy_time") or datetime.now(timezone.utc),
            "reconcile_source": f"terminal_{side}_fallback",
        })
        for key in (
            "pending_buy_order_id",
            "pending_buy_quantity",
            "pending_sell_order_id",
            "sell_order_id",
            "sell_quantity",
            "broker_quantity_before_order",
        ):
            previous.pop(key, None)
        open_trades[symbol] = previous
        return dict(previous)


def _finalize_filled_buy(symbol: str, order, order_id: str) -> None:
    qty = _safe_float(getattr(order, "filled_qty", 0), 0)
    price = _safe_float(getattr(order, "filled_avg_price", 0), 0)
    synced, _ = _sync_local_position_from_broker(
        symbol,
        source="terminal_buy_broker_snapshot",
    )
    if not synced:
        _fallback_terminal_position_update(
            symbol,
            side="buy",
            filled_qty=qty,
            filled_price=price,
        )

    app_state.setdefault("last_trade_price_by_symbol", {})[symbol] = price
    app_state["last_trade_time"] = time.time()
    app_state["last_signal"] = "buy"

    clear_entry_lock(symbol)

    logging.info(f"[✓ Filled BUY] {symbol}: {qty} @ ${price:.2f}")


def _finalize_filled_sell(symbol: str, order, order_id: str) -> None:
    qty = _safe_float(getattr(order, "filled_qty", 0), 0)
    price = _safe_float(getattr(order, "filled_avg_price", 0), 0)
    sell_time = datetime.now(timezone.utc)

    trade_info = app_state.setdefault("open_trades", {}).get(symbol)

    if trade_info:
        buy_price = _safe_float(trade_info.get("buy_price", price), price)
        buy_time = trade_info.get("buy_time", sell_time)
        profit_loss = float(price) - float(buy_price)

        trade_type = app_state["strategy"].get("last_exit_reason", "Standard")
        log_trade_to_summary(symbol, buy_price, buy_time, price, sell_time, trade_type)
        app_state["strategy"]["last_exit_reason"] = "Standard"

        app_state["strategy"].setdefault("last_sell_price_by_symbol", {})[symbol] = price
        logging.info(f"[SellTrack] 💾 Stored last sell price for {symbol}: {price:.2f}")

        if profit_loss < 0:
            app_state["strategy"]["consecutive_losses"] += 1
            logging.warning(
                f"[Loss Tracker] ❌ Loss recorded. Total consecutive losses: "
                f"{app_state['strategy']['consecutive_losses']}"
            )
            if app_state["strategy"]["consecutive_losses"] >= 3:
                app_state["strategy"]["cooldown_until"] = time.time() + 300
                logging.warning("[Cooldown] 🧊 Triggered 5-minute cooldown due to 3+ consecutive losses")
                app_state["strategy"]["consecutive_losses"] = 0
        else:
            app_state["strategy"]["consecutive_losses"] = 0
    else:
        logging.warning(f"[OrderMonitor] ⚠️ Filled SELL for {symbol} but no matching open_trades entry was found.")

    synced, _ = _sync_local_position_from_broker(
        symbol,
        source="terminal_sell_broker_snapshot",
    )
    if not synced:
        _fallback_terminal_position_update(
            symbol,
            side="sell",
            filled_qty=qty,
            filled_price=price,
        )

    app_state["last_trade_time"] = time.time()
    app_state["last_signal"] = "sell"
    app_state["strategy"].setdefault("sells_in_progress", set()).discard(symbol)
    clear_entry_lock(symbol)

    logging.info(f"[✓ Filled SELL] {symbol}: {qty} @ ${price:.2f}")

def track_limit_order(
    symbol,
    order_id,
    side=None,
    qty=None,
    limit_price=None,
    market_is_open=None,
    submission_context: dict | None = None,
):
    """
    Track a submitted order with enough metadata to support:
    - duplicate blocking
    - extended-hours cancellation/replacement
    - later submission-to-fill diagnostics

    submission_context is diagnostic-only and must not change
    order-monitor behavior.
    """
    tracked = {
        "order_id": str(
            order_id
        ),
        "side": (
            normalize_side(side)
            if side is not None
            else None
        ),
        "qty": _safe_float(
            qty,
            0,
        ),
        "limit_price": _safe_float(
            limit_price,
            0,
        ),
        "market_is_open": (
            bool(market_is_open)
            if market_is_open
            is not None
            else None
        ),
        "tracked_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    if isinstance(
        submission_context,
        dict,
    ):
        tracked.update(
            dict(
                submission_context
            )
        )

    app_state.setdefault(
        "open_orders",
        {},
    )[symbol] = tracked

    logging.info(
        "[TRACK] Tracking order | "
        "symbol=%s id=%s side=%s qty=%s "
        "limit_price=%s market_is_open=%s "
        "cycle_id=%s plan_id=%s row_id=%s "
        "reference_source=%s",
        symbol,
        order_id,
        side,
        qty,
        limit_price,
        market_is_open,
        tracked.get(
            "cycle_id"
        ),
        tracked.get(
            "plan_id"
        ),
        tracked.get(
            "row_id"
        ),
        tracked.get(
            "submission_reference_source"
        ),
    )

    if not app_state.get(
        "monitoring_orders",
        False,
    ):
        app_state[
            "monitoring_orders"
        ] = True

        safe_thread(
            _order_monitor_thread_entry,
            name="OrderMonitor",
            daemon=True,
        )


def get_tracked_order(symbol: str) -> dict | None:
    entry = app_state.setdefault("open_orders", {}).get(symbol)
    return entry if isinstance(entry, dict) else None


def clear_tracked_order(symbol: str) -> None:
    app_state.setdefault("open_orders", {}).pop(symbol, None)


def _parse_tracked_at(value):
    if not value:
        return None

    try:
        text = str(value)
        if text.endswith("Z"):
            text = text.replace("Z", "+00:00")

        dt = datetime.fromisoformat(text)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        return None


def _order_age_seconds(tracked: dict) -> float:
    tracked_at = _parse_tracked_at(tracked.get("tracked_at")) if isinstance(tracked, dict) else None

    if not tracked_at:
        return 0.0

    return max(0.0, (datetime.now(timezone.utc) - tracked_at).total_seconds())


def _cleanup_local_order_state_after_cancel(symbol: str, side: str | None) -> None:
    """
    Remove local tracking for an order we intentionally canceled.

    Important:
    - For canceled BUY orders, remove pending local open_trades entries only.
    - Do not remove real filled/synced positions.
    - For canceled SELL orders, clear sell guards so future sells are not blocked forever.
    """
    side = normalize_side(side) if side else None

    clear_tracked_order(symbol)

    if side == "buy":
        clear_entry_lock(symbol)

        trade_info = app_state.get("open_trades", {}).get(symbol)
        if isinstance(trade_info, dict):
            status = str(trade_info.get("status", "")).lower().strip()
            if status == "pending":
                app_state["open_trades"].pop(symbol, None)
            else:
                trade_info.pop("pending_buy_order_id", None)
                trade_info.pop("pending_buy_quantity", None)
                trade_info.pop("broker_quantity_before_order", None)

    elif side == "sell":
        app_state.setdefault("strategy", {}).setdefault("sells_in_progress", set()).discard(symbol)
        trade_info = app_state.get("open_trades", {}).get(symbol)
        if isinstance(trade_info, dict):
            trade_info["status"] = "synced"
            trade_info.pop("pending_sell_order_id", None)
            trade_info.pop("sell_order_id", None)
            trade_info.pop("sell_quantity", None)
            trade_info.pop("broker_quantity_before_order", None)


def cancel_tracked_order(symbol: str, reason: str = "manual_cleanup") -> bool:
    """
    Request cancellation for one tracked broker order.

    Important:
    Do not immediately clear local tracking after a cancel request.
    The order may briefly remain pending_cancel, or it could still fill before
    the broker confirms cancellation. The monitor loop should finalize the real
    terminal state.
    """
    tracked = get_tracked_order(symbol)

    if not isinstance(tracked, dict):
        logging.info("[OrderCleanup] No tracked order found for %s.", symbol)
        return False

    order_id = tracked.get("order_id")
    side = tracked.get("side")

    if not order_id:
        logging.warning("[OrderCleanup] %s tracked order missing order_id; clearing local state.", symbol)
        _cleanup_local_order_state_after_cancel(symbol, side)
        return True

    try:
        app_state["trading_client"].cancel_order_by_id(order_id)

        tracked["cancel_requested"] = True
        tracked["cancel_reason"] = reason
        tracked["cancel_requested_at"] = datetime.now(timezone.utc).isoformat()

        logging.warning(
            "[OrderCleanup] Requested cancel for tracked order | symbol=%s side=%s order_id=%s reason=%s",
            symbol,
            side,
            order_id,
            reason,
        )

        return True

    except Exception as e:
        message = str(e).lower()

        # If broker already closed/removed it, local tracking is stale and should be cleared.
        if "not found" in message or "404" in message:
            logging.warning(
                "[OrderCleanup] Broker order not found for %s; clearing local stale order. order_id=%s",
                symbol,
                order_id,
            )
            _cleanup_local_order_state_after_cancel(symbol, side)
            return True

        logging.warning(
            "[OrderCleanup] Failed to request cancel for %s; keeping tracking for now. order_id=%s error=%s",
            symbol,
            order_id,
            e,
        )
        return False


def cancel_stale_extended_limit_orders_if_market_open() -> int:
    """
    Cancel extended-hours limit orders that survived into regular market hours.

    This prevents a premarket limit order from blocking the symbol all day after
    price has moved away from the stale limit.
    """
    if not CANCEL_EXTENDED_LIMITS_WHEN_MARKET_OPENS:
        return 0

    client = app_state.get("trading_client")
    if not client:
        return 0

    try:
        clock = client.get_clock()
        market_is_open = bool(getattr(clock, "is_open", False))
    except Exception as e:
        logging.warning("[OrderCleanup] Could not check market clock: %s", e)
        return 0

    if not market_is_open:
        return 0

    canceled = 0

    for symbol, tracked in list(app_state.setdefault("open_orders", {}).items()):
        if not isinstance(tracked, dict):
            continue

        was_created_outside_market = tracked.get("market_is_open") is False
        age_seconds = _order_age_seconds(tracked)

        if not was_created_outside_market:
            continue

        if age_seconds < EXTENDED_LIMIT_ORDER_MAX_AGE_SECONDS:
            continue

        order_id = tracked.get("order_id")

        try:
            order = client.get_order_by_id(order_id)
            status = normalize_status(getattr(order, "status", ""))
            order_type = str(getattr(order, "type", "")).lower()
        except Exception as e:
            logging.warning(
                "[OrderCleanup] Could not fetch tracked order for stale check | symbol=%s order_id=%s error=%s",
                symbol,
                order_id,
                e,
            )
            continue

        if status in {"filled", "canceled", "expired", "rejected", "done_for_day"}:
            continue

        if "limit" not in order_type:
            continue

        if cancel_tracked_order(symbol, reason="stale_extended_limit_survived_market_open"):
            canceled += 1

    if canceled:
        logging.warning("[OrderCleanup] Canceled %s stale extended-hours limit order(s) after market open.", canceled)

    return canceled


def get_open_order_for_symbol_side(symbol: str, side: str):
    """
    Return the first broker-side open order matching symbol + side, else None.
    """
    try:
        params = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        open_orders = app_state["trading_client"].get_orders(filter=params)

        wanted_side = normalize_side(side)
        for order in open_orders:
            order_symbol = getattr(order, "symbol", None)
            order_side = normalize_side(getattr(order, "side", ""))

            if order_symbol == symbol and order_side == wanted_side:
                return order

    except Exception as e:
        logging.warning(f"[OrderLookup] Failed checking open orders for {symbol}/{side}: {e}")

    return None


def should_replace_limit_order(existing_limit_price: float, new_limit_price: float, threshold_pct: float = 0.0025) -> bool:
    """
    Replace only if price changed by at least threshold_pct.
    Default 0.25%.
    """
    existing_limit_price = _safe_float(existing_limit_price, 0)
    new_limit_price = _safe_float(new_limit_price, 0)

    if existing_limit_price <= 0 or new_limit_price <= 0:
        return True

    pct_diff = abs(new_limit_price - existing_limit_price) / existing_limit_price
    return pct_diff >= threshold_pct


def reconcile_existing_order(symbol: str, side, new_price: float, new_qty: float, market_is_open: bool, replace_threshold_pct: float = 0.0025) -> tuple[str, str | None]:
    """
    Decide whether to submit a new order, keep an existing one, or request a replace.

    Returns:
        ("submit_new", None)
        ("keep_existing", existing_order_id)
        ("replace_required", existing_order_id)
        ("blocked_has_position", existing_order_id or None)
        ("blocked_no_position", existing_order_id or None)
    """
    side_str = normalize_side(side)

    # BUY safety: never place a buy if we already have/pending a position
    if side_str == "buy":
        trade_info = app_state.get("open_trades", {}).get(symbol)
        if trade_info and str(trade_info.get("status", "")).lower().strip() in {"pending", "filled", "pending_sell", "synced"}:
            return "blocked_has_position", str(trade_info.get("order_id")) if trade_info.get("order_id") else None

        try:
            positions = app_state["trading_client"].get_all_positions()
            if any(getattr(p, "symbol", None) == symbol and _safe_float(getattr(p, "qty", 0)) > 0 for p in positions):
                return "blocked_has_position", None
        except Exception as e:
            logging.warning(f"[ReconcileOrder] Could not verify live buy position for {symbol}: {e}")

    # SELL safety: never place a sell if we do not hold shares
    if side_str == "sell":
        try:
            positions = app_state["trading_client"].get_all_positions()
            has_position = any(
                getattr(p, "symbol", None) == symbol and _safe_float(getattr(p, "qty", 0)) > 0
                for p in positions
            )
            if not has_position:
                return "blocked_no_position", None
        except Exception as e:
            logging.warning(f"[ReconcileOrder] Could not verify live sell position for {symbol}: {e}")

    existing_order = get_open_order_for_symbol_side(symbol, side_str)
    if not existing_order:
        return "submit_new", None

    existing_order_id = str(getattr(existing_order, "id", ""))
    existing_status = str(getattr(existing_order, "status", "")).lower()
    existing_type = str(getattr(existing_order, "type", "")).lower()
    existing_limit_price = _safe_float(getattr(existing_order, "limit_price", 0), 0)

    if existing_status in {"pending_cancel", "pending_replace"}:
        logging.info(
            f"[ReconcileOrder] Existing {side_str} order for {symbol} is already transitioning "
            f"(status={existing_status}); keeping it for now: {existing_order_id}"
        )
        return "keep_existing", existing_order_id

    # Market hours: just block duplicates. No replace logic needed.
    if market_is_open:
        logging.info(f"[ReconcileOrder] Keeping existing in-hours {side_str} order for {symbol}: {existing_order_id}")
        return "keep_existing", existing_order_id

    # Extended hours: only limit orders should be in play
    if "limit" not in existing_type:
        logging.info(f"[ReconcileOrder] Keeping existing non-limit {side_str} order for {symbol}: {existing_order_id}")
        return "keep_existing", existing_order_id

    if not should_replace_limit_order(existing_limit_price, new_price, threshold_pct=replace_threshold_pct):
        logging.info(
            f"[ReconcileOrder] Keeping existing {side_str} limit order for {symbol} "
            f"(old={existing_limit_price:.2f}, new={new_price:.2f})"
        )
        return "keep_existing", existing_order_id

    logging.info(
        f"[ReconcileOrder] Replacement required for {side_str} order on {symbol}: "
        f"old={existing_limit_price:.2f}, new={new_price:.2f}"
    )
    return "replace_required", existing_order_id

async def monitor_open_orders_loop() -> None:
    """
    Polls open_orders every 5 seconds and updates open_trades when filled.
    Stops when all orders are resolved OR shutdown_event is set.
    """
    client = app_state["trading_client"]
    shutdown_event = app_state["stream"].get("shutdown_event")

    app_state.setdefault("open_orders", {})
    app_state.setdefault("open_trades", {})

    try:
        while True:
            # Exit if shutdown requested
            if shutdown_event and shutdown_event.is_set():
                logging.info("[OrderMonitor] Exiting due to shutdown_event.")
                return

            # Exit if nothing to monitor
            if not app_state["open_orders"]:
                return
            
            cancel_stale_extended_limit_orders_if_market_open()

            if not app_state["open_orders"]:
                return

            for symbol, tracked in list(app_state["open_orders"].items()):
                if shutdown_event and shutdown_event.is_set():
                    logging.info("[OrderMonitor] Exiting due to shutdown_event.")
                    return

                try:
                    if isinstance(tracked, dict):
                        order_id = tracked.get("order_id")
                        tracked_side = normalize_side(tracked.get("side"))
                    else:
                        # backward compatibility
                        order_id = tracked
                        tracked_side = None

                    if not order_id:
                        logging.warning(f"[OrderMonitor] Missing order_id for tracked order on {symbol}; removing.")
                        clear_entry_lock(symbol)
                        del app_state["open_orders"][symbol]
                        try:
                            reconcile_fail_safe(client=client)
                        except Exception:
                            logging.warning(
                                "[FailSafeLifecycle] Missing-order reconciliation failed "
                                "for %s",
                                symbol,
                                exc_info=True,
                            )
                        continue

                    order = client.get_order_by_id(order_id)
                    status = normalize_status(getattr(order, "status", ""))
                    record_order_update(
                        symbol,
                        order,
                        status,
                        lifecycle_id=(
                            tracked.get("fail_safe_lifecycle_id")
                            if isinstance(tracked, dict)
                            else None
                        ),
                    )
                    logging.debug(f"[OrderMonitor] {symbol} → {status}")

                    if status == "filled":
                        _record_terminal_order_outcome(
                            symbol=symbol,
                            tracked=tracked,
                            order=order,
                            status=status,
                        )

                        if tracked_side == "buy":
                            _finalize_filled_buy(symbol, order, order_id)

                        elif tracked_side == "sell":
                            _finalize_filled_sell(symbol, order, order_id)

                        else:
                            # backward-compat fallback: infer from broker order side
                            broker_side = normalize_side(getattr(order, "side", ""))
                            if broker_side == "buy":
                                _finalize_filled_buy(symbol, order, order_id)
                            elif broker_side == "sell":
                                _finalize_filled_sell(symbol, order, order_id)
                            else:
                                logging.warning(
                                    f"[OrderMonitor] Filled order for {symbol} had unknown side; "
                                    f"defaulting to buy-style tracking. order_id={order_id}"
                                )
                                _finalize_filled_buy(symbol, order, order_id)

                        del app_state["open_orders"][symbol]
                        try:
                            reconcile_fail_safe(client=client)
                        except Exception:
                            logging.warning(
                                "[FailSafeLifecycle] Post-fill reconciliation failed "
                                "for %s",
                                symbol,
                                exc_info=True,
                            )

                    elif status in (
                        "canceled",
                        "expired",
                        "rejected",
                        "done_for_day",
                    ):
                        _record_terminal_order_outcome(
                            symbol=symbol,
                            tracked=tracked,
                            order=order,
                            status=status,
                        )

                        _cleanup_local_order_state_after_cancel(
                            symbol,
                            tracked_side,
                        )
                        try:
                            reconcile_fail_safe(client=client)
                        except Exception:
                            logging.warning(
                                "[FailSafeLifecycle] Terminal-failure reconciliation "
                                "failed for %s",
                                symbol,
                                exc_info=True,
                            )
                        logging.warning(f"[✖️ OrderClosed] {symbol} → {status.upper()} — removed from tracking")

                    else:
                        continue

                except Exception as e:
                    logging.warning(f"[⚠️ OrderMonitor] {symbol} → Order check failed: {e}")

            # interruptible sleep (checks shutdown_event every ~0.25s)
            loop = asyncio.get_running_loop()
            total = 5.0
            step = 0.25
            end = loop.time() + total

            while loop.time() < end:
                if shutdown_event and shutdown_event.is_set():
                    logging.info("[OrderMonitor] Exiting due to shutdown_event.")
                    return
                await asyncio.sleep(min(step, end - loop.time()))

    finally:
        app_state["monitoring_orders"] = False
        logging.info("✅ No more open orders — order monitoring stopped.")

def create_order_request(symbol, qty, side, price, market_is_open):
    price = float(f"{price:.2f}")
    if market_is_open:
        logging.info(f"📈 Using Market Order for {str(side).upper()} (Market open)")
        return MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY)
    else:
        logging.info(f"🌙 Using Limit Order for {str(side).upper()} (Extended Hours)")
        return LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            limit_price=price,
            time_in_force=TimeInForce.DAY,
            extended_hours=True,
        )

def check_position_status(symbol: str) -> tuple[bool, bool]:
    try:
        trade_info = app_state.get("open_trades", {}).get(symbol)
        logging.debug(f"[PositionStatus] open_trades for {symbol}: {trade_info}")

        if isinstance(trade_info, dict):
            status = str(trade_info.get("status", "")).lower().strip()

            # Treat only truly held states as a position
            if status in {"filled", "pending_sell", "synced"}:
                has_pending_sell = _has_broker_pending_sell(symbol)
                logging.debug(f"[PositionStatus] {symbol}: local status={status}, has_pending_sell={has_pending_sell}")
                return True, not has_pending_sell

            # Pending buy is not yet a true held position
            if status == "pending":
                logging.debug(f"[PositionStatus] {symbol}: local status=pending (not held yet)")
                return False, True

        positions = app_state["trading_client"].get_all_positions()
        logging.debug(f"[PositionStatus] Alpaca positions: {[p.symbol for p in positions]}")

        has_position = any(
            getattr(pos, "symbol", None) == symbol and _safe_float(getattr(pos, "qty", 0), 0) > 0
            for pos in positions
        )

        if has_position:
            has_pending_sell = _has_broker_pending_sell(symbol)
            logging.debug(f"[PositionStatus] {symbol}: broker has_position={has_position}, has_pending_sell={has_pending_sell}")
            return True, not has_pending_sell

        return False, True

    except Exception as e:
        logging.warning(f"⚠️ Fallback position check failed for {symbol}: {e}")
        return False, True

def check_local_position(symbol, open_trades) -> tuple[bool, bool]:
    trade_info = open_trades.get(symbol)
    if not isinstance(trade_info, dict):
        return False, True

    status = str(trade_info.get("status", "")).lower().strip()

    # Only these statuses should count as actually holding
    has_position = status in {"filled", "pending_sell", "synced"}
    if not has_position:
        return False, True

    try:
        params = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        open_orders = app_state["trading_client"].get_orders(filter=params)
        has_pending_sell = any(
            getattr(o, "symbol", None) == symbol
            and normalize_side(getattr(o, "side", "")) == "sell"
            for o in open_orders
        )
        can_sell = not has_pending_sell
    except Exception as e:
        logging.warning(f"⚠️ Error checking open orders for {symbol}: {e}")
        can_sell = False

    return has_position, can_sell


def should_block_extended_order_near_open(buffer_seconds: int = 15 * 60) -> tuple[bool, str]:
    """
    Prevent new extended-hours limit orders shortly before regular market open.

    This avoids submitting a premarket limit order that immediately becomes stale
    when the market opens and price moves away.
    """
    client = app_state.get("trading_client")
    if not client:
        return True, "missing_trading_client"

    try:
        clock = client.get_clock()
    except Exception as e:
        return True, f"clock_check_failed: {e}"

    if bool(getattr(clock, "is_open", False)):
        return False, "market_open"

    now = datetime.now(timezone.utc)
    next_open = getattr(clock, "next_open", None)

    if not next_open:
        return False, "next_open_unavailable"

    if next_open.tzinfo is None:
        next_open = next_open.replace(tzinfo=timezone.utc)

    seconds_until_open = (next_open.astimezone(timezone.utc) - now).total_seconds()

    if 0 <= seconds_until_open <= buffer_seconds:
        return True, f"market_opens_in_{int(seconds_until_open)}s"

    return False, "safe_extended_hours_window"
