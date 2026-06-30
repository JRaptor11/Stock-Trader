# layer4_executor.py

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from alpaca.trading.enums import OrderSide, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from core.state import app_state, fail_safe_event
from config import runtime_config as config
from utils.orders_utils import (
    create_order_request,
    normalize_side,
    track_limit_order,
)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _normalize_decision(value: Any) -> str:
    return str(value or "").upper().strip()


def _normalize_symbol(value: Any) -> str:
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


def _extract_layer4_order_values(row: dict) -> dict:
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
    symbol = _normalize_symbol(row.get("symbol"))
    decision = _normalize_decision(row.get("decision"))

    qty = _safe_float(
        row.get(
            "remaining_authorized_qty",
            row.get("max_authorized_qty", row.get("planned_qty", row.get("qty", 0.0))),
        ),
        0.0,
    )

    price = _safe_float(
        row.get("live_price", row.get("price", 0.0)),
        0.0,
    )

    notional = _safe_float(
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
        logging.warning("[Layer4Exec] Could not fetch broker positions.", exc_info=True)
        return out

    for pos in positions:
        symbol = _normalize_symbol(getattr(pos, "symbol", ""))
        qty = _safe_float(getattr(pos, "qty", 0), 0.0)

        if symbol and qty > 0:
            out[symbol] = qty

    return out


def _available_cash(client) -> float:
    try:
        account = client.get_account()
        return _safe_float(getattr(account, "cash", 0), 0.0)
    except Exception:
        logging.warning("[Layer4Exec] Could not fetch account cash.", exc_info=True)
        return 0.0


def _executable_rows(plan: Any) -> list[dict]:
    rows = _get_plan_rows(plan)
    executable = []

    for row in rows:
        values = _extract_layer4_order_values(row)
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
            -abs(_safe_float(r.get("notional"), 0.0)),
        )
    )

    return executable


def execute_layer4_plan(plan: Any, summary: dict | None = None) -> dict:
    """
    Execute the latest Layer 3 plan using Layer 4.

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

    summary = summary or {}
    cycle_id = summary.get("cycle_id")
    plan_id = summary.get("plan_id")

    result = {
        "layer": "layer4",
        "mode": "direct_compat",
        "cycle_id": cycle_id,
        "plan_id": plan_id,
        "enabled": _execution_enabled(),
        "attempted": 0,
        "submitted": 0,
        "skipped": 0,
        "errors": 0,
        "blocked_reason": None,
        "orders": [],
    }

    layers = app_state.setdefault("layers", {})
    layer4_state = layers.setdefault("layer4_execution", {})
    layer4_state["last_attempted_at"] = datetime.now(timezone.utc).isoformat()
    layer4_state["last_plan_id"] = plan_id

    if not _execution_enabled():
        logging.info(
            "[Layer4Exec] Execution disabled; dry-run only. cycle_id=%s plan_id=%s",
            cycle_id,
            plan_id,
        )
        result["blocked_reason"] = "execution_disabled"
        layer4_state["last_result"] = result
        return result

    if fail_safe_event.is_set() or app_state.get("fail_safes", {}).get("state"):
        logging.warning(
            "[Layer4Exec] Blocked because fail-safe is active. cycle_id=%s plan_id=%s",
            cycle_id,
            plan_id,
        )
        result["blocked_reason"] = "fail_safe_active"
        layer4_state["last_result"] = result
        return result

    client = app_state.get("trading_client")
    if client is None:
        logging.warning("[Layer4Exec] No trading_client available.")
        result["blocked_reason"] = "missing_trading_client"
        layer4_state["last_result"] = result
        return result

    try:
        clock = client.get_clock()
        market_is_open = bool(getattr(clock, "is_open", False))
    except Exception:
        logging.warning("[Layer4Exec] Could not fetch market clock.", exc_info=True)
        result["blocked_reason"] = "clock_fetch_failed"
        layer4_state["last_result"] = result
        return result

    if _market_hours_only() and not market_is_open:
        logging.info(
            "[Layer4Exec] Market is closed and market-hours-only execution is enabled. "
            "Skipping execution. cycle_id=%s plan_id=%s",
            cycle_id,
            plan_id,
        )
        result["blocked_reason"] = "market_closed"
        layer4_state["last_result"] = result
        return result

    try:
        open_orders = _broker_open_orders(client)
    except Exception:
        logging.warning("[Layer4Exec] Could not fetch open orders.", exc_info=True)
        result["blocked_reason"] = "open_order_fetch_failed"
        layer4_state["last_result"] = result
        return result

    if open_orders:
        logging.warning(
            "[Layer4Exec] Existing broker open orders detected; skipping Layer 4 "
            "execution this cycle. open_order_count=%s cycle_id=%s plan_id=%s",
            len(open_orders),
            cycle_id,
            plan_id,
        )
        result["blocked_reason"] = "broker_open_orders_exist"
        layer4_state["last_result"] = result
        return result

    executable = _executable_rows(plan)
    if not executable:
        logging.info(
            "[Layer4Exec] No executable BUY/SELL rows. cycle_id=%s plan_id=%s",
            cycle_id,
            plan_id,
        )
        result["blocked_reason"] = "no_executable_rows"
        layer4_state["last_result"] = result
        return result

    logging.info(
        "[Layer4Exec] Executable rows found | cycle_id=%s plan_id=%s count=%s rows=%s",
        cycle_id,
        plan_id,
        len(executable),
        [
            {
                "symbol": r.get("symbol"),
                "decision": r.get("decision"),
                "qty": r.get("qty"),
                "price": r.get("price"),
                "notional": r.get("notional"),
                "reason": r.get("reason"),
            }
            for r in executable
        ],
    )

    position_qty = _position_qty_by_symbol(client)

    for row in executable:
        symbol = row["symbol"]
        decision = row["decision"]
        qty = _safe_float(row["qty"], 0.0)
        price = _safe_float(row["price"], 0.0)
        notional = _safe_float(row.get("notional"), qty * price)
        reason = str(row.get("reason", ""))
        row_id = row.get("row_id")

        result["attempted"] += 1

        if decision == "SELL":
            held_qty = position_qty.get(symbol, 0.0)
            if held_qty <= 0:
                logging.info(
                    "[Layer4Exec] SELL skipped for %s; broker shows no shares held.",
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
            side = OrderSide.SELL

        elif decision == "BUY":
            cash = _available_cash(client)
            if notional > cash:
                logging.info(
                    "[Layer4Exec] BUY skipped for %s; notional %.2f exceeds cash %.2f.",
                    symbol,
                    notional,
                    cash,
                )
                result["skipped"] += 1
                result["orders"].append(
                    {
                        "symbol": symbol,
                        "side": "buy",
                        "status": "skipped",
                        "reason": "insufficient_cash",
                        "notional": notional,
                        "cash": cash,
                        "plan_id": plan_id,
                        "row_id": row_id,
                    }
                )
                continue

            side = OrderSide.BUY

        else:
            result["skipped"] += 1
            continue

        try:
            order_request = create_order_request(
                symbol=symbol,
                qty=qty,
                side=side,
                price=price,
                market_is_open=market_is_open,
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
                    "source": "layer4",
                    "layer4_reason": reason,
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
                existing["source"] = "layer4"
                existing["layer4_reason"] = reason
                existing["cycle_id"] = cycle_id
                existing["plan_id"] = plan_id
                existing["row_id"] = row_id

            logging.warning(
                "[Layer4Exec] Submitted %s %s qty=%s notional=$%.2f "
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

            result["submitted"] += 1
            result["orders"].append(
                {
                    "symbol": symbol,
                    "side": decision.lower(),
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
            logging.exception(
                "[Layer4Exec] Failed submitting %s for %s. cycle_id=%s plan_id=%s",
                decision,
                symbol,
                cycle_id,
                plan_id,
            )
            result["errors"] += 1
            result["orders"].append(
                {
                    "symbol": symbol,
                    "side": decision.lower(),
                    "status": "error",
                    "error": str(exc),
                    "qty": qty,
                    "notional": notional,
                    "price": price,
                    "reason": reason,
                    "plan_id": plan_id,
                    "row_id": row_id,
                }
            )

    layer4_state["last_cycle_id"] = cycle_id
    layer4_state["last_plan_id"] = plan_id
    layer4_state["last_result"] = result

    logging.warning(
        "[Layer4Exec] Complete | cycle_id=%s plan_id=%s attempted=%s submitted=%s "
        "skipped=%s errors=%s",
        cycle_id,
        plan_id,
        result["attempted"],
        result["submitted"],
        result["skipped"],
        result["errors"],
    )

    return result