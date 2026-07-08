# layers/layer5_executor.py

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from alpaca.trading.enums import OrderSide, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from layers.layer_logging import (
    compact_executable_row_for_log,
    compact_orders_for_log,
)
from utils.numeric import safe_float, safe_round
from utils.symbols import normalize_symbol

from core.state import app_state, fail_safe_event
from config import runtime_config as config
from trading.orders import (
    create_order_request,
    normalize_side,
    track_limit_order,
)

from layers.layer_csv import append_layer4_order_rows


LAYER5_BROKER_ERROR_COOLDOWN_SECONDS = 30 * 60
LAYER5_QUANTITY_ERROR_CODES = {"40310000", 40310000}


def _normalize_decision(value: Any) -> str:
    return str(value or "").upper().strip()


def _get_plan_rows(plan: Any) -> List[Dict[str, Any]]:
    """
    Accept either:
    - a list of row dicts
    - a dict containing {"plan": [...]}
    - a dict containing {"rows": [...]}
    - a full active_execution_plan containing {"rows": [...]}
    """
    if isinstance(plan, list):
        return [row for row in plan if isinstance(row, dict)]

    if isinstance(plan, dict):
        for key in ("rows", "plan", "decisions"):
            rows = plan.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]

    return []


def _extract_layer5_order_values(row: dict) -> dict:
    """
    Normalize plan-row fields for Layer 4 execution.

    Layer 3 now produces Layer-4-ready fields:
    - max_authorized_qty
    - max_authorized_notional
    - remaining_authorized_qty
    - remaining_authorized_notional
    - live_price

    This also supports older Layer 3 fields:
    - planned_qty
    - planned_notional
    - qty / notional / price
    """
    symbol = normalize_symbol(row.get("symbol"))
    decision = _normalize_decision(row.get("decision"))

    qty = safe_float(
        row.get(
            "remaining_authorized_qty",
            row.get("max_authorized_qty", row.get("planned_qty", row.get("qty", 0.0))),
        ),
        0.0,
    )

    price = safe_float(
        row.get("live_price", row.get("price", 0.0)),
        0.0,
    )

    notional = safe_float(
        row.get(
            "remaining_authorized_notional",
            row.get("max_authorized_notional", row.get("planned_notional", row.get("notional", 0.0))),
        ),
        0.0,
    )

    if notional <= 0 and qty > 0 and price > 0:
        notional = qty * price

    return {
        "symbol": symbol,
        "decision": decision,
        "qty": qty,
        "price": price,
        "notional": notional,
    }


def _execution_enabled() -> bool:
    """
    During the transition, Layer 4 uses the existing Layer 3 execution flag by default.

    Optional future override:
    - app_state["execution"]["layer4_execution_enabled"]
    - config.LAYER4_EXECUTION_ENABLED
    """
    execution_state = app_state.get("execution", {})

    if "layer4_execution_enabled" in execution_state:
        return bool(execution_state.get("layer4_execution_enabled"))

    if hasattr(config, "LAYER4_EXECUTION_ENABLED"):
        return bool(getattr(config, "LAYER4_EXECUTION_ENABLED"))

    return bool(
        execution_state.get(
            "layer3_execution_enabled",
            getattr(config, "LAYER3_EXECUTION_ENABLED", False),
        )
    )


def _market_hours_only() -> bool:
    execution_state = app_state.get("execution", {})

    if "layer4_market_hours_only" in execution_state:
        return bool(execution_state.get("layer4_market_hours_only"))

    if hasattr(config, "LAYER4_MARKET_HOURS_ONLY"):
        return bool(getattr(config, "LAYER4_MARKET_HOURS_ONLY"))

    return bool(
        execution_state.get(
            "layer3_market_hours_only",
            getattr(config, "LAYER3_MARKET_HOURS_ONLY", True),
        )
    )


def _broker_open_orders(client) -> list:
    params = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    return list(client.get_orders(filter=params))


def _position_qty_by_symbol(client) -> dict[str, float]:
    out: dict[str, float] = {}

    try:
        positions = client.get_all_positions()
    except Exception:
        logging.warning("[Layer5Exec] Could not fetch broker positions.", exc_info=True)
        return out

    for pos in positions:
        symbol = normalize_symbol(getattr(pos, "symbol", ""))
        qty = safe_float(getattr(pos, "qty", 0), 0.0)

        if symbol and qty > 0:
            out[symbol] = qty

    return out


def _available_cash(client) -> float:
    try:
        account = client.get_account()
        return safe_float(getattr(account, "cash", 0), 0.0)
    except Exception:
        logging.warning("[Layer5Exec] Could not fetch account cash.", exc_info=True)
        return 0.0


def _serialize_enum_or_value(value: Any) -> str | None:
    if value is None:
        return None

    return str(getattr(value, "value", value))


def _refresh_broker_snapshot(client, layer5_state: dict, *, label: str) -> dict:
    snapshot = {
        "label": label,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "account_ok": False,
        "positions_ok": False,
        "equity": None,
        "cash": None,
        "buying_power": None,
        "positions": {},
        "error": None,
    }

    try:
        account = client.get_account()
        snapshot["account_ok"] = True
        snapshot["equity"] = safe_float(getattr(account, "equity", 0.0), 0.0)
        snapshot["cash"] = safe_float(getattr(account, "cash", 0.0), 0.0)
        snapshot["buying_power"] = safe_float(
            getattr(account, "buying_power", 0.0),
            0.0,
        )
    except Exception as exc:
        snapshot["error"] = str(exc)
        logging.warning(
            "[Layer5Exec] Failed refreshing broker account snapshot | label=%s",
            label,
            exc_info=True,
        )

    try:
        positions = client.get_all_positions()
        snapshot["positions_ok"] = True

        for pos in positions:
            symbol = normalize_symbol(getattr(pos, "symbol", ""))
            if not symbol:
                continue

            snapshot["positions"][symbol] = {
                "qty": safe_float(getattr(pos, "qty", 0.0), 0.0),
                "market_value": safe_float(getattr(pos, "market_value", 0.0), 0.0),
                "avg_entry_price": safe_float(
                    getattr(pos, "avg_entry_price", 0.0),
                    0.0,
                ),
            }

    except Exception as exc:
        snapshot["error"] = str(exc)
        logging.warning(
            "[Layer5Exec] Failed refreshing broker positions snapshot | label=%s",
            label,
            exc_info=True,
        )

    layer5_state[f"broker_snapshot_{label}"] = snapshot
    layer5_state["last_broker_snapshot"] = snapshot

    return snapshot


def _broker_error_cooldowns() -> dict:
    return app_state.setdefault("execution", {}).setdefault(
        "layer5_broker_error_cooldowns",
        {},
    )


def _broker_error_cooldown_remaining(symbol: str) -> float:
    cooldowns = _broker_error_cooldowns()
    until = safe_float(cooldowns.get(symbol), 0.0)

    if until <= 0:
        cooldowns.pop(symbol, None)
        return 0.0

    remaining = until - time.time()
    if remaining <= 0:
        cooldowns.pop(symbol, None)
        return 0.0

    return remaining


def _set_broker_error_cooldown(symbol: str, *, code: Any = None, message: str | None = None) -> float:
    until = time.time() + LAYER5_BROKER_ERROR_COOLDOWN_SECONDS
    _broker_error_cooldowns()[symbol] = until

    logging.warning(
        "[Layer5Exec] Broker-error cooldown set | symbol=%s seconds=%s code=%s message=%s",
        symbol,
        LAYER5_BROKER_ERROR_COOLDOWN_SECONDS,
        code,
        message,
    )

    return until


def _clear_broker_error_cooldown(symbol: str) -> None:
    _broker_error_cooldowns().pop(symbol, None)


def _json_payload_from_exception_text(text: str) -> dict:
    text = str(text or "").strip()

    if not text:
        return {}

    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        pass

    match = re.search(r"\{.*\}", text)
    if match:
        try:
            payload = json.loads(match.group(0))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    return {}


def _extract_broker_error_details(exc: Exception) -> dict:
    text = str(exc)
    payload = _json_payload_from_exception_text(text)

    code = payload.get("code")
    message = payload.get("message") or text

    return {
        "broker_error_code": code,
        "broker_error_message": message,
        "broker_error_available_qty": payload.get("available"),
        "broker_error_existing_qty": payload.get("existing_qty"),
        "broker_error_held_for_orders": payload.get("held_for_orders"),
        "broker_error_symbol": normalize_symbol(payload.get("symbol")),
        "broker_error_raw": text,
    }


def _is_quantity_availability_error(details: dict) -> bool:
    code = details.get("broker_error_code")
    message = str(details.get("broker_error_message") or "").lower()

    return code in LAYER5_QUANTITY_ERROR_CODES or (
        "insufficient qty available" in message
    )


def _executable_rows(plan: Any) -> list[dict]:
    rows = _get_plan_rows(plan)
    executable = []

    for row in rows:
        values = _extract_layer5_order_values(row)
        decision = values["decision"]
        symbol = values["symbol"]
        qty = values["qty"]
        notional = values["notional"]
        price = values["price"]

        if decision not in {"BUY", "SELL"}:
            continue

        if not symbol:
            continue

        if qty <= 0:
            continue

        if price <= 0:
            continue

        executable.append(
            {
                **row,
                "decision": decision,
                "symbol": symbol,
                "qty": qty,
                "notional": notional,
                "price": price,
            }
        )

    # Sells first, then buys. Within each side, larger notional first.
    executable.sort(
        key=lambda r: (
            0 if r["decision"] == "SELL" else 1,
            -abs(safe_float(r.get("notional"), 0.0)),
        )
    )

    return executable


def _finish_layer5_result(
    *,
    result: dict,
    layer5_state: dict,
    started_monotonic: float,
    log_level: int = logging.INFO,
) -> dict:
    """
    Store and log the final Layer 4 execution result from every return path.
    """
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    result["duration_seconds"] = round(time.monotonic() - started_monotonic, 3)

    attempted = int(result.get("attempted", 0) or 0)
    submitted = int(result.get("submitted", 0) or 0)
    skipped = int(result.get("skipped", 0) or 0)
    errors = int(result.get("errors", 0) or 0)

    result["count_integrity_ok"] = attempted == submitted + skipped + errors

    cycle_id = result.get("cycle_id")
    plan_id = result.get("plan_id")

    layer5_state["last_cycle_id"] = cycle_id
    layer5_state["last_plan_id"] = plan_id
    layer5_state["last_result"] = result

    logging.log(
        log_level,
        "[Layer5Exec] Complete | cycle_id=%s plan_id=%s attempted=%s "
        "submitted=%s skipped=%s errors=%s blocked_reason=%s "
        "duration=%.3fs count_integrity_ok=%s",
        cycle_id,
        plan_id,
        attempted,
        submitted,
        skipped,
        errors,
        result.get("blocked_reason"),
        result.get("duration_seconds"),
        result.get("count_integrity_ok"),
    )

    compact_orders = compact_orders_for_log(result.get("orders", []))

    if compact_orders:
        logging.warning(
            "[Layer5Exec] Orders summary | cycle_id=%s plan_id=%s orders=%s",
            cycle_id,
            plan_id,
            compact_orders,
        )

    append_layer4_order_rows(result)

    return result


def execute_layer5_plan(plan: Any, summary: dict | None = None) -> dict:
    """
    Execute the latest Layer 4 plan using Layer 5.

    Current compatibility behavior:
    - submits orders immediately, just like the old Layer 3 executor did
    - does not yet slice orders throughout the cycle
    - does not yet time entries based on ticks/pullbacks

    Future behavior:
    - work the authorized plan over its TTL window
    - track partial fills
    - time orders intracycle
    - expire/reconcile stale plans at the next Layer 3 cycle
    """
    started_monotonic = time.monotonic()

    started_at = datetime.now(timezone.utc).isoformat()

    summary = summary or {}
    cycle_id = summary.get("cycle_id")
    plan_id = summary.get("plan_id")

    execution_enabled = _execution_enabled()

    result = {
        "layer": "layer5",
        "mode": "direct_compat_execution",
        "cycle_id": cycle_id,
        "plan_id": plan_id,
        "enabled": execution_enabled,
        "started_at": started_at,
        "finished_at": None,
        "duration_seconds": None,
        "attempted": 0,
        "submitted": 0,
        "skipped": 0,
        "errors": 0,
        "blocked_reason": None,
        "count_integrity_ok": None,
        "orders": [],
    }

    layers = app_state.setdefault("layers", {})
    layer5_state = layers.setdefault("layer5_execution", {})
    layer5_state["last_attempted_at"] = datetime.now(timezone.utc).isoformat()
    layer5_state["last_plan_id"] = plan_id

    if not execution_enabled:
        logging.info(
            "[Layer5Exec] Execution disabled; dry-run only. cycle_id=%s plan_id=%s",
            cycle_id,
            plan_id,
        )
        result["blocked_reason"] = "execution_disabled"
        return _finish_layer5_result(
            result=result,
            layer5_state=layer5_state,
            started_monotonic=started_monotonic,
        )

    if fail_safe_event.is_set() or app_state.get("fail_safes", {}).get("state"):
        logging.warning(
            "[Layer5Exec] Blocked because fail-safe is active. cycle_id=%s plan_id=%s",
            cycle_id,
            plan_id,
        )
        result["blocked_reason"] = "fail_safe_active"
        return _finish_layer5_result(
            result=result,
            layer5_state=layer5_state,
            started_monotonic=started_monotonic,
            log_level=logging.WARNING,
        )

    client = app_state.get("trading_client")
    if client is None:
        logging.warning("[Layer5Exec] No trading_client available.")
        result["blocked_reason"] = "missing_trading_client"
        return _finish_layer5_result(
            result=result,
            layer5_state=layer5_state,
            started_monotonic=started_monotonic,
            log_level=logging.WARNING,
        )

    try:
        clock = client.get_clock()
        market_is_open = bool(getattr(clock, "is_open", False))
    except Exception:
        logging.warning("[Layer5Exec] Could not fetch market clock.", exc_info=True)
        result["blocked_reason"] = "clock_fetch_failed"
        return _finish_layer5_result(
            result=result,
            layer5_state=layer5_state,
            started_monotonic=started_monotonic,
            log_level=logging.WARNING,
        )

    if _market_hours_only() and not market_is_open:
        logging.info(
            "[Layer5Exec] Market is closed and market-hours-only execution is enabled. "
            "Skipping execution. cycle_id=%s plan_id=%s",
            cycle_id,
            plan_id,
        )
        result["blocked_reason"] = "market_closed"
        return _finish_layer5_result(
            result=result,
            layer5_state=layer5_state,
            started_monotonic=started_monotonic,
        )

    try:
        open_orders = _broker_open_orders(client)
    except Exception:
        logging.warning("[Layer5Exec] Could not fetch open orders.", exc_info=True)
        result["blocked_reason"] = "open_order_fetch_failed"
        return _finish_layer5_result(
            result=result,
            layer5_state=layer5_state,
            started_monotonic=started_monotonic,
            log_level=logging.WARNING,
        )

    open_order_count_by_symbol = {}

    for order in open_orders:
        order_symbol = normalize_symbol(getattr(order, "symbol", ""))
        if not order_symbol:
            continue

        open_order_count_by_symbol[order_symbol] = (
            open_order_count_by_symbol.get(order_symbol, 0) + 1
        )

    if open_orders:
        logging.warning(
            "[Layer5Exec] Existing broker open orders detected; skipping Layer 4 "
            "execution this cycle. open_order_count=%s cycle_id=%s plan_id=%s",
            len(open_orders),
            cycle_id,
            plan_id,
        )
        result["blocked_reason"] = "broker_open_orders_exist"
        return _finish_layer5_result(
            result=result,
            layer5_state=layer5_state,
            started_monotonic=started_monotonic,
            log_level=logging.WARNING,
        )

    executable = _executable_rows(plan)
    if not executable:
        logging.info(
            "[Layer5Exec] No executable BUY/SELL rows. cycle_id=%s plan_id=%s",
            cycle_id,
            plan_id,
        )
        result["blocked_reason"] = "no_executable_rows"
        return _finish_layer5_result(
            result=result,
            layer5_state=layer5_state,
            started_monotonic=started_monotonic,
        )

    logging.info(
        "[Layer5Exec] Executable rows found | cycle_id=%s plan_id=%s count=%s rows=%s",
        cycle_id,
        plan_id,
        len(executable),
        [compact_executable_row_for_log(row) for row in executable],
    )

    position_qty = _position_qty_by_symbol(client)
    _refresh_broker_snapshot(client, layer5_state, label="pre_execution")
    available_cash_budget = _available_cash(client)

    for row in executable:
        symbol = row["symbol"]
        decision = row["decision"]
        qty = safe_float(row["qty"], 0.0)
        price = safe_float(row["price"], 0.0)
        notional = safe_float(row.get("notional"), qty * price)
        reason = str(row.get("reason", ""))
        row_id = row.get("row_id") or f"{plan_id}:{symbol}"

        result["attempted"] += 1

        logging.info(
            "[Layer5Exec] Row start | cycle_id=%s plan_id=%s row_id=%s "
            "symbol=%s decision=%s qty=%s notional=$%.2f price=$%.2f reason=%s",
            cycle_id,
            plan_id,
            row_id,
            symbol,
            decision,
            qty,
            notional,
            price,
            reason,
        )

        cooldown_remaining = _broker_error_cooldown_remaining(symbol)
        if cooldown_remaining > 0:
            logging.warning(
                "[Layer5Exec] Skipping %s because broker-error cooldown is active for %.0fs. "
                "cycle_id=%s plan_id=%s row_id=%s",
                symbol,
                cooldown_remaining,
                cycle_id,
                plan_id,
                row_id,
            )
            result["skipped"] += 1
            result["orders"].append(
                {
                    "symbol": symbol,
                    "side": decision.lower(),
                    "status": "skipped",
                    "reason": "broker_error_cooldown",
                    "cooldown_remaining_seconds": round(cooldown_remaining, 1),
                    "qty": qty,
                    "notional": notional,
                    "price": price,
                    "plan_id": plan_id,
                    "row_id": row_id,
                }
            )
            continue

        cash_budget_before = available_cash_budget
        position_qty_before = position_qty.get(symbol, 0.0)
        open_order_count_for_symbol = open_order_count_by_symbol.get(symbol, 0)

        if decision == "SELL":
            held_qty = position_qty.get(symbol, 0.0)
            if held_qty <= 0:
                logging.info(
                    "[Layer5Exec] SELL skipped for %s; broker shows no shares held.",
                    symbol,
                )
                result["skipped"] += 1
                result["orders"].append(
                    {
                        "symbol": symbol,
                        "side": "sell",
                        "status": "skipped",
                        "reason": "no_broker_position",
                        "plan_id": plan_id,
                        "row_id": row_id,
                    }
                )
                continue

            qty = min(qty, held_qty)
            notional = qty * price
            side = OrderSide.SELL

        elif decision == "BUY":
            if notional > available_cash_budget:
                logging.info(
                    "[Layer5Exec] BUY skipped for %s; notional %.2f exceeds remaining cash budget %.2f.",
                    symbol,
                    notional,
                    available_cash_budget,
                )
                result["skipped"] += 1
                result["orders"].append(
                    {
                        "symbol": symbol,
                        "side": "buy",
                        "status": "skipped",
                        "reason": "insufficient_cash_budget",
                        "notional": notional,
                        "cash": available_cash_budget,
                        "cash_budget_before": cash_budget_before,
                        "cash_budget_after": available_cash_budget,
                        "position_qty_before": position_qty_before,
                        "open_order_count_for_symbol": open_order_count_for_symbol,
                        "plan_id": plan_id,
                        "row_id": row_id,
                    }
                )
                continue

            available_cash_budget -= notional
            side = OrderSide.BUY

        else:
            result["skipped"] += 1
            continue

        computed_side = normalize_side(side)
        order_request = None
        order_request_side = None
        order_type = None
        order_time_in_force = None
        cash_budget_after = available_cash_budget

        try:
            order_request = create_order_request(
                symbol=symbol,
                qty=qty,
                side=side,
                price=price,
                market_is_open=market_is_open,
            )

            order_request_side = normalize_side(getattr(order_request, "side", side))
            order_type = type(order_request).__name__
            order_time_in_force = _serialize_enum_or_value(
                getattr(order_request, "time_in_force", None)
            )

            logging.warning(
                "[Layer5Exec] Broker order request | cycle_id=%s plan_id=%s row_id=%s "
                "symbol=%s decision=%s computed_side=%s request_side=%s qty=%s "
                "price=%s market_is_open=%s order_type=%s time_in_force=%s request=%s",
                cycle_id,
                plan_id,
                row_id,
                symbol,
                decision,
                computed_side,
                order_request_side,
                qty,
                price,
                market_is_open,
                order_type,
                order_time_in_force,
                order_request,
            )

            if order_request_side != decision.lower():
                raise RuntimeError(
                    f"Order side mismatch before submit: decision={decision.lower()} "
                    f"request_side={order_request_side} symbol={symbol} row_id={row_id}"
                )

            submitted_order = client.submit_order(order_request)
            order_id = getattr(submitted_order, "id", None)

            if not order_id:
                raise RuntimeError(f"Broker did not return order id for {symbol}")

            track_limit_order(
                symbol=symbol,
                order_id=order_id,
                side=side,
                qty=qty,
                limit_price=price if not market_is_open else None,
                market_is_open=market_is_open,
            )

            if normalize_side(side) == "buy":
                app_state.setdefault("open_trades", {})[symbol] = {
                    "status": "pending",
                    "order_id": str(order_id),
                    "quantity": qty,
                    "buy_price": price,
                    "buy_time": datetime.now(timezone.utc),
                    "source": "layer5",
                    "layer4_reason": reason,  # temporary backward-compatible field
                    "layer5_reason": reason,
                    "cycle_id": cycle_id,
                    "plan_id": plan_id,
                    "row_id": row_id,
                }

            elif normalize_side(side) == "sell":
                existing = app_state.setdefault("open_trades", {}).setdefault(
                    symbol,
                    {},
                )
                existing["status"] = "pending_sell"
                existing["sell_order_id"] = str(order_id)
                existing["sell_quantity"] = qty
                existing["source"] = "layer5"
                existing["layer4_reason"] = reason  # temporary backward-compatible field
                existing["layer5_reason"] = reason
                existing["cycle_id"] = cycle_id
                existing["plan_id"] = plan_id
                existing["row_id"] = row_id

            logging.warning(
                "[Layer5Exec] Submitted %s %s qty=%s notional=$%.2f "
                "price=$%.2f reason=%s order_id=%s cycle_id=%s plan_id=%s",
                decision,
                symbol,
                qty,
                notional,
                price,
                reason,
                order_id,
                cycle_id,
                plan_id,
            )

            _clear_broker_error_cooldown(symbol)

            result["submitted"] += 1
            result["orders"].append(
                {
                    "symbol": symbol,
                    "side": decision.lower(),
                    "submitted_side": computed_side,
                    "order_request_side": order_request_side,
                    "order_type": order_type,
                    "time_in_force": order_time_in_force,
                    "market_is_open": market_is_open,
                    "position_qty_before": position_qty_before,
                    "open_order_count_for_symbol": open_order_count_for_symbol,
                    "cash_budget_before": cash_budget_before,
                    "cash_budget_after": cash_budget_after,
                    "status": "submitted",
                    "order_id": str(order_id),
                    "qty": qty,
                    "notional": notional,
                    "price": price,
                    "reason": reason,
                    "plan_id": plan_id,
                    "row_id": row_id,
                }
            )

        except Exception as exc:
            broker_error_details = _extract_broker_error_details(exc)

            logging.exception(
                "[Layer5Exec] Failed submitting %s for %s. cycle_id=%s plan_id=%s "
                "computed_side=%s request_side=%s broker_code=%s broker_message=%s",
                decision,
                symbol,
                cycle_id,
                plan_id,
                computed_side,
                order_request_side,
                broker_error_details.get("broker_error_code"),
                broker_error_details.get("broker_error_message"),
            )

            cooldown_until = None
            if _is_quantity_availability_error(broker_error_details):
                cooldown_until = _set_broker_error_cooldown(
                    symbol,
                    code=broker_error_details.get("broker_error_code"),
                    message=broker_error_details.get("broker_error_message"),
                )

            result["errors"] += 1
            result["orders"].append(
                {
                    "symbol": symbol,
                    "side": decision.lower(),
                    "submitted_side": computed_side,
                    "order_request_side": order_request_side,
                    "order_type": order_type,
                    "time_in_force": order_time_in_force,
                    "market_is_open": market_is_open,
                    "status": "error",
                    "error": str(exc),
                    "broker_error_code": broker_error_details.get("broker_error_code"),
                    "broker_error_message": broker_error_details.get("broker_error_message"),
                    "broker_error_existing_qty": broker_error_details.get("broker_error_existing_qty"),
                    "broker_error_available_qty": broker_error_details.get("broker_error_available_qty"),
                    "broker_error_held_for_orders": broker_error_details.get("broker_error_held_for_orders"),
                    "broker_error_symbol": broker_error_details.get("broker_error_symbol"),
                    "broker_error_raw": broker_error_details.get("broker_error_raw"),
                    "cooldown_until": cooldown_until,
                    "qty": qty,
                    "notional": notional,
                    "price": price,
                    "reason": reason,
                    "position_qty_before": position_qty_before,
                    "open_order_count_for_symbol": open_order_count_for_symbol,
                    "cash_budget_before": cash_budget_before,
                    "cash_budget_after": cash_budget_after,
                    "plan_id": plan_id,
                    "row_id": row_id,
                }
            )

    _refresh_broker_snapshot(client, layer5_state, label="post_execution")

    return _finish_layer5_result(
        result=result,
        layer5_state=layer5_state,
        started_monotonic=started_monotonic,
        log_level=logging.WARNING if result["errors"] else logging.INFO,
    )