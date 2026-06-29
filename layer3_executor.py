# layer3_executor.py

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from alpaca.trading.enums import OrderSide, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from state import app_state, fail_safe_event
from utils import config_utils as config
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


def _extract_layer3_order_values(row: dict) -> dict:
    """
    Normalize Layer 3 plan row fields for execution.

    Layer 3 planner rows currently use:
    - planned_qty
    - planned_notional
    - live_price

    Older/simple executor logic may expect:
    - qty
    - notional
    - price

    This function supports both.
    """
    symbol = str(row.get("symbol", "") or "").upper().strip()
    decision = str(row.get("decision", "") or "").upper().strip()

    qty = _safe_float(
        row.get("planned_qty", row.get("qty", 0.0)),
        0.0,
    )

    price = _safe_float(
        row.get("live_price", row.get("price", 0.0)),
        0.0,
    )

    notional = _safe_float(
        row.get("planned_notional", row.get("notional", 0.0)),
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
    """
    if isinstance(plan, list):
        return [row for row in plan if isinstance(row, dict)]

    if isinstance(plan, dict):
        for key in ("plan", "rows", "decisions"):
            rows = plan.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]

    return []


def _execution_enabled() -> bool:
    return bool(
        app_state.get("execution", {}).get(
            "layer3_execution_enabled",
            getattr(config, "LAYER3_EXECUTION_ENABLED", False),
        )
    )


def _market_hours_only() -> bool:
    return bool(
        app_state.get("execution", {}).get(
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
        logging.warning("[Layer3Exec] Could not fetch broker positions.", exc_info=True)
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
        logging.warning("[Layer3Exec] Could not fetch account cash.", exc_info=True)
        return 0.0


def _executable_rows(plan: Any) -> list[dict]:
    rows = _get_plan_rows(plan)
    executable = []

    for row in rows:
        values = _extract_layer3_order_values(row)

        decision = values["decision"]
        symbol = values["symbol"]
        qty = values["qty"]
        price = values["price"]
        notional = values["notional"]

        if decision not in {"BUY", "SELL"}:
            continue

        if not symbol or qty <= 0 or price <= 0 or notional <= 0:
            logging.warning(
                "[Layer3Exec] Skipping non-executable row | "
                "symbol=%s decision=%s qty=%s price=%s notional=%s "
                "reason=%s row_keys=%s",
                symbol,
                decision,
                qty,
                price,
                notional,
                row.get("reason"),
                sorted(row.keys()),
            )
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

    # Sells first, then buys.
    # Within each side, larger notional first.
    executable.sort(
        key=lambda r: (
            0 if r["decision"] == "SELL" else 1,
            -abs(_safe_float(r.get("notional"), 0.0)),
        )
    )

    return executable


def execute_layer3_plan(plan: Any, summary: dict | None = None) -> dict:
    """
    Submit Alpaca paper orders from Layer 3 plan rows.

    Layer 3 planner decides:
    - symbol
    - BUY/SELL
    - qty
    - notional
    - reason

    This executor only decides:
    - whether execution is enabled
    - whether final safety checks pass
    - whether to submit and track orders
    """

    summary = summary or {}
    cycle_id = summary.get("cycle_id")

    result = {
        "cycle_id": cycle_id,
        "enabled": _execution_enabled(),
        "attempted": 0,
        "submitted": 0,
        "skipped": 0,
        "errors": 0,
        "blocked_reason": None,
        "orders": [],
    }

    app_state.setdefault("layers", {}).setdefault("execution", {})[
        "last_attempted_at"
    ] = datetime.now(timezone.utc).isoformat()

    if not _execution_enabled():
        logging.info(
            "[Layer3Exec] Execution disabled; dry-run only. cycle_id=%s",
            cycle_id,
        )
        result["blocked_reason"] = "execution_disabled"
        app_state["layers"]["execution"]["last_result"] = result
        return result

    if fail_safe_event.is_set() or app_state.get("fail_safes", {}).get("state"):
        logging.warning(
            "[Layer3Exec] Blocked because fail-safe is active. cycle_id=%s",
            cycle_id,
        )
        result["blocked_reason"] = "fail_safe_active"
        app_state["layers"]["execution"]["last_result"] = result
        return result

    client = app_state.get("trading_client")
    if client is None:
        logging.warning("[Layer3Exec] No trading_client available.")
        result["blocked_reason"] = "missing_trading_client"
        app_state["layers"]["execution"]["last_result"] = result
        return result

    try:
        clock = client.get_clock()
        market_is_open = bool(getattr(clock, "is_open", False))
    except Exception:
        logging.warning("[Layer3Exec] Could not fetch market clock.", exc_info=True)
        result["blocked_reason"] = "clock_fetch_failed"
        app_state["layers"]["execution"]["last_result"] = result
        return result

    if _market_hours_only() and not market_is_open:
        logging.info(
            "[Layer3Exec] Market is closed and LAYER3_MARKET_HOURS_ONLY=true. "
            "Skipping execution. cycle_id=%s",
            cycle_id,
        )
        result["blocked_reason"] = "market_closed"
        app_state["layers"]["execution"]["last_result"] = result
        return result

    try:
        open_orders = _broker_open_orders(client)
    except Exception:
        logging.warning("[Layer3Exec] Could not fetch open orders.", exc_info=True)
        result["blocked_reason"] = "open_order_fetch_failed"
        app_state["layers"]["execution"]["last_result"] = result
        return result

    if open_orders:
        logging.warning(
            "[Layer3Exec] Existing broker open orders detected; skipping Layer 3 "
            "execution this cycle. open_order_count=%s cycle_id=%s",
            len(open_orders),
            cycle_id,
        )
        result["blocked_reason"] = "broker_open_orders_exist"
        app_state["layers"]["execution"]["last_result"] = result
        return result

    executable = _executable_rows(plan)

    logging.info(
        "[Layer3Exec] Executable rows found | cycle_id=%s count=%s rows=%s",
        cycle_id,
        len(executable),
        [
            {
                "symbol": row.get("symbol"),
                "decision": row.get("decision"),
                "qty": row.get("qty"),
                "price": row.get("price"),
                "notional": row.get("notional"),
                "reason": row.get("reason"),
            }
            for row in executable
        ],
    )

    if not executable:
        logging.info("[Layer3Exec] No executable BUY/SELL rows. cycle_id=%s", cycle_id)
        result["blocked_reason"] = "no_executable_rows"
        app_state["layers"]["execution"]["last_result"] = result
        return result

    position_qty = _position_qty_by_symbol(client)

    for row in executable:
        symbol = row["symbol"]
        decision = row["decision"]
        qty = _safe_float(row["qty"], 0.0)
        price = _safe_float(row["price"], 0.0)
        notional = _safe_float(row.get("notional"), qty * price)
        reason = str(row.get("reason", ""))

        result["attempted"] += 1

        if decision == "SELL":
            held_qty = position_qty.get(symbol, 0.0)
            if held_qty <= 0:
                logging.info(
                    "[Layer3Exec] SELL skipped for %s; broker shows no shares held.",
                    symbol,
                )
                result["skipped"] += 1
                result["orders"].append(
                    {
                        "symbol": symbol,
                        "side": "sell",
                        "status": "skipped",
                        "reason": "no_broker_position",
                    }
                )
                continue

            qty = min(qty, held_qty)
            side = OrderSide.SELL

        elif decision == "BUY":
            cash = _available_cash(client)
            if notional > cash:
                logging.info(
                    "[Layer3Exec] BUY skipped for %s; notional %.2f exceeds cash %.2f.",
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
                    "source": "layer3",
                    "layer3_reason": reason,
                    "cycle_id": cycle_id,
                }

            elif normalize_side(side) == "sell":
                existing = app_state.setdefault("open_trades", {}).setdefault(
                    symbol,
                    {},
                )
                existing["status"] = "pending_sell"
                existing["sell_order_id"] = str(order_id)
                existing["sell_quantity"] = qty
                existing["source"] = "layer3"
                existing["layer3_reason"] = reason
                existing["cycle_id"] = cycle_id

            logging.warning(
                "[Layer3Exec] Submitted %s %s qty=%s notional=$%.2f "
                "price=$%.2f reason=%s order_id=%s cycle_id=%s",
                decision,
                symbol,
                qty,
                notional,
                price,
                reason,
                order_id,
                cycle_id,
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
                }
            )

        except Exception as exc:
            logging.exception(
                "[Layer3Exec] Failed submitting %s for %s. cycle_id=%s",
                decision,
                symbol,
                cycle_id,
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
                }
            )

    app_state["layers"]["execution"]["last_cycle_id"] = cycle_id
    app_state["layers"]["execution"]["last_result"] = result

    logging.warning(
        "[Layer3Exec] Complete | cycle_id=%s attempted=%s submitted=%s "
        "skipped=%s errors=%s",
        cycle_id,
        result["attempted"],
        result["submitted"],
        result["skipped"],
        result["errors"],
    )

    return result