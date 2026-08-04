# layers/layer5_executor.py

import json
import logging
import re
import time
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from alpaca.trading.enums import OrderSide, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from layers.layer_logging import (
    compact_executable_row_for_log,
    compact_orders_for_log,
)
from utils.numeric import safe_float, safe_round
from utils.symbols import normalize_symbol

from core.state import app_state
from config import runtime_config as config
from trading.orders import (
    create_order_request,
    normalize_side,
    register_layer5_cycle_submissions,
    track_limit_order,
)

from layers.layer_csv import append_layer4_order_rows
from safety.fail_safe_lifecycle import (
    eligible_for_submission,
    mark_submission_failed,
    mark_submission_started,
    mark_submitted,
    snapshot as fail_safe_lifecycle_snapshot,
)


LAYER5_BROKER_ERROR_COOLDOWN_SECONDS = 30 * 60
LAYER5_QUANTITY_ERROR_CODES = {"40310000", 40310000}
_LAYER5_EXECUTION_LOCK = threading.RLock()
EASTERN = ZoneInfo("America/New_York")


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


def _config_value(name: str, default: Any) -> Any:
    value = app_state.get("config_overrides", {}).get(
        name,
        app_state.get("config_defaults", {}).get(name, default),
    )
    return default if value is None else value


def _fail_safe_after_hours_window(
    now: datetime | None = None,
) -> tuple[bool, str]:
    if not bool(_config_value("FAIL_SAFE_EXTENDED_HOURS_ENABLED", True)):
        return False, "extended_fail_safe_disabled"
    local = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
    minutes = local.hour * 60 + local.minute
    if local.weekday() >= 5:
        return False, "weekend"
    if 16 * 60 <= minutes < 20 * 60:
        return True, "after_hours_4pm_8pm_et"
    return False, "outside_supported_after_hours_window"


def _fail_safe_extended_quote(symbol: str) -> dict:
    client = app_state.get("stock_data_client")
    if client is None:
        return {"ok": False, "reason": "missing_stock_data_client"}
    try:
        from alpaca.data.requests import StockLatestQuoteRequest

        response = client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=[symbol])
        )
        quote = (
            response.get(symbol)
            if isinstance(response, dict)
            else getattr(response, "data", {}).get(symbol)
        )
        if quote is None:
            return {"ok": False, "reason": "missing_quote"}
        bid = safe_float(getattr(quote, "bid_price", 0), 0.0)
        ask = safe_float(getattr(quote, "ask_price", 0), 0.0)
        timestamp = getattr(quote, "timestamp", None)
        if bid <= 0 or ask <= 0 or ask < bid:
            return {"ok": False, "reason": "invalid_bid_ask", "bid": bid, "ask": ask}
        now = datetime.now(timezone.utc)
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            age = max(0.0, (now - timestamp.astimezone(timezone.utc)).total_seconds())
        else:
            age = None
        max_age = float(_config_value(
            "FAIL_SAFE_EXTENDED_HOURS_MAX_QUOTE_AGE_SECONDS", 15.0
        ))
        if age is None or age > max_age:
            return {
                "ok": False,
                "reason": "stale_quote",
                "bid": bid,
                "ask": ask,
                "quote_age_seconds": age,
            }
        midpoint = (bid + ask) / 2.0
        spread_pct = (ask - bid) / midpoint * 100 if midpoint > 0 else 0.0
        max_spread = float(_config_value(
            "FAIL_SAFE_EXTENDED_HOURS_MAX_SPREAD_PCT", 1.5
        ))
        if spread_pct > max_spread:
            return {
                "ok": False,
                "reason": "spread_too_wide",
                "bid": bid,
                "ask": ask,
                "spread_pct": spread_pct,
                "quote_age_seconds": age,
            }
        offset_bps = float(_config_value(
            "FAIL_SAFE_EXTENDED_HOURS_LIMIT_OFFSET_BPS", 5.0
        ))
        limit_price = max(0.01, bid * (1.0 - offset_bps / 10000.0))
        return {
            "ok": True,
            "reason": "fresh_quote",
            "bid": bid,
            "ask": ask,
            "spread_pct": spread_pct,
            "quote_timestamp": timestamp.isoformat(),
            "quote_age_seconds": age,
            "limit_offset_bps": offset_bps,
            "limit_price": round(limit_price, 2),
        }
    except Exception as exc:
        logging.warning(
            "[Layer5Exec] Extended-hours fail-safe quote failed | symbol=%s error=%s",
            symbol,
            exc,
            exc_info=True,
        )
        return {"ok": False, "reason": "quote_fetch_failed", "error": str(exc)}


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


def _serialize_datetime_or_value(
    value: Any,
) -> str | None:
    if value is None:
        return None

    if isinstance(
        value,
        datetime,
    ):
        if value.tzinfo is None:
            value = value.replace(
                tzinfo=timezone.utc
            )
        else:
            value = value.astimezone(
                timezone.utc
            )

        return value.isoformat()

    text = str(
        value
    ).strip()

    return text or None


def _submission_price_context(
    symbol: str,
    plan_price: float,
    *,
    captured_at: datetime | None = None,
) -> dict:
    """
    Capture diagnostic-only price context immediately before
    broker submission.

    This is not a bid/ask quote. The preferred reference is the
    latest streamed trade retained by MarketDataBuffer.
    """
    captured_at = (
        captured_at
        or datetime.now(
            timezone.utc
        )
    )

    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(
            tzinfo=timezone.utc
        )
    else:
        captured_at = captured_at.astimezone(
            timezone.utc
        )

    plan_price = safe_float(
        plan_price,
        0.0,
    )

    reference_price = 0.0
    reference_source = None
    tick_timestamp = None
    reference_age_seconds = None

    md = app_state.get(
        "market_data",
        {},
    ).get(
        "buffer"
    )

    if (
        md is not None
        and hasattr(
            md,
            "get_recent_prices_ts",
        )
    ):
        try:
            points = list(
                md.get_recent_prices_ts(
                    symbol,
                    limit=1,
                )
                or []
            )
        except Exception:
            points = []

            logging.debug(
                "[Layer5Exec] Could not read "
                "submission-time streamed price "
                "for %s.",
                symbol,
                exc_info=True,
            )

        if points:
            raw_timestamp, raw_price = (
                points[-1]
            )

            candidate_price = safe_float(
                raw_price,
                0.0,
            )

            try:
                event_epoch = float(
                    raw_timestamp
                )
            except Exception:
                event_epoch = None

            if candidate_price > 0:
                reference_price = (
                    candidate_price
                )
                reference_source = (
                    "latest_streamed_trade"
                )

                if event_epoch is not None:
                    tick_dt = datetime.fromtimestamp(
                        event_epoch,
                        tz=timezone.utc,
                    )

                    tick_timestamp = (
                        tick_dt.isoformat()
                    )

                    reference_age_seconds = round(
                        max(
                            0.0,
                            (
                                captured_at
                                - tick_dt
                            ).total_seconds(),
                        ),
                        3,
                    )

    if reference_price <= 0:
        state_price = safe_float(
            app_state.get(
                "last_trade_price_by_symbol",
                {},
            ).get(
                symbol
            ),
            0.0,
        )

        if state_price > 0:
            reference_price = state_price
            reference_source = (
                "app_state_last_trade"
            )

    if (
        reference_price <= 0
        and plan_price > 0
    ):
        reference_price = plan_price
        reference_source = (
            "plan_price_fallback"
        )

    drift_pct = None

    if (
        reference_price > 0
        and plan_price > 0
    ):
        drift_pct = round(
            (
                reference_price
                - plan_price
            )
            / plan_price,
            8,
        )

    return {
        "submission_context_captured_at": (
            captured_at.isoformat()
        ),
        "submission_plan_price": (
            round(
                plan_price,
                6,
            )
            if plan_price > 0
            else None
        ),
        "submission_reference_price": (
            round(
                reference_price,
                6,
            )
            if reference_price > 0
            else None
        ),
        "submission_reference_source": (
            reference_source
            or "unavailable"
        ),
        "submission_reference_tick_timestamp": (
            tick_timestamp
        ),
        "submission_reference_age_seconds": (
            reference_age_seconds
        ),
        "submission_reference_vs_plan_pct": (
            drift_pct
        ),

        "broker_submit_started_at": None,
        "broker_submit_completed_at": None,
        "broker_submit_latency_ms": None,

        "broker_status_at_submit": None,
        "broker_created_at": None,
        "broker_submitted_at": None,
        "broker_limit_price": None,
    }


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


def _wait_for_sell_terminal_states(
    client,
    order_ids: list[str],
    *,
    timeout_seconds: float = 12.0,
    poll_seconds: float = 0.25,
) -> dict:
    """Wait briefly for submitted sells before refreshing cash for buys."""
    result = {
        "requested_order_count": len(order_ids),
        "terminal_order_count": 0,
        "timed_out_order_ids": [],
        "duration_seconds": 0.0,
        "supported": callable(getattr(client, "get_order_by_id", None)),
    }
    if not order_ids or not result["supported"]:
        return result

    terminal_statuses = {
        "filled", "canceled", "expired", "rejected", "done_for_day",
    }
    pending = {str(order_id) for order_id in order_ids if order_id}
    started = time.monotonic()
    deadline = started + max(0.0, float(timeout_seconds))

    while pending and time.monotonic() < deadline:
        for order_id in list(pending):
            try:
                order = client.get_order_by_id(order_id)
                status = str(
                    getattr(
                        getattr(order, "status", ""),
                        "value",
                        getattr(order, "status", ""),
                    )
                    or ""
                ).lower().strip()
                if status in terminal_statuses:
                    pending.discard(order_id)
            except Exception:
                logging.warning(
                    "[Layer5Exec] Sell-phase order refresh failed | order_id=%s",
                    order_id,
                    exc_info=True,
                )
        if pending:
            time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    result["duration_seconds"] = round(time.monotonic() - started, 3)
    result["terminal_order_count"] = len(order_ids) - len(pending)
    result["timed_out_order_ids"] = sorted(pending)
    return result


def _broker_error_cooldowns() -> dict:
    return app_state.setdefault("execution", {}).setdefault(
        "layer5_broker_error_cooldowns",
        {},
    )


def _fail_safe_snapshot() -> dict:
    return fail_safe_lifecycle_snapshot()


def _fail_safe_liquidation_symbols(snapshot: dict, position_qty: dict[str, float]) -> list[str]:
    if not snapshot.get("active"):
        return []

    if snapshot.get("liquidate_all"):
        return sorted(symbol for symbol, qty in position_qty.items() if qty > 0)

    symbols = set(snapshot.get("pending_liquidation_symbols") or [])
    symbols.update(snapshot.get("symbols") or [])

    symbol = normalize_symbol(snapshot.get("symbol"))
    if symbol:
        symbols.add(symbol)

    return sorted(
        symbol
        for symbol in symbols
        if position_qty.get(symbol, 0.0) > 0
        and eligible_for_submission(symbol)
    )


def _price_for_fail_safe_sell(symbol: str, existing_rows: list[dict]) -> float:
    for row in existing_rows:
        if normalize_symbol(row.get("symbol")) == symbol:
            price = safe_float(row.get("price", row.get("live_price")), 0.0)
            if price > 0:
                return price

    price = safe_float(
        app_state.get("last_trade_price_by_symbol", {}).get(symbol),
        0.0,
    )
    if price > 0:
        return price

    trade_info = app_state.get("open_trades", {}).get(symbol, {})
    if isinstance(trade_info, dict):
        return safe_float(trade_info.get("buy_price"), 0.0)

    return 0.0


def _append_fail_safe_liquidation_rows(
    executable: list[dict],
    *,
    snapshot: dict,
    position_qty: dict[str, float],
    cycle_id: Any,
    plan_id: Any,
    market_is_open: bool,
) -> list[dict]:
    sell_symbols = {
        normalize_symbol(row.get("symbol"))
        for row in executable
        if _normalize_decision(row.get("decision")) == "SELL"
    }

    added_rows: list[dict] = []

    for symbol in _fail_safe_liquidation_symbols(snapshot, position_qty):
        if symbol in sell_symbols:
            for row in executable:
                if (
                    normalize_symbol(row.get("symbol")) == symbol
                    and _normalize_decision(row.get("decision")) == "SELL"
                ):
                    qty = safe_float(position_qty.get(symbol), 0.0)
                    price = _price_for_fail_safe_sell(symbol, executable)
                    extended_context = None
                    if not market_is_open:
                        extended_context = _fail_safe_extended_quote(symbol)
                        if not extended_context.get("ok"):
                            logging.warning(
                                "[Layer5Exec] Existing fail-safe SELL deferred "
                                "during extended hours | symbol=%s reason=%s "
                                "context=%s",
                                symbol,
                                extended_context.get("reason"),
                                extended_context,
                            )
                            executable.remove(row)
                            break
                        price = safe_float(
                            extended_context.get("limit_price"),
                            0.0,
                        )
                    row.update(
                        {
                            "qty": qty,
                            "notional": qty * price,
                            "price": price,
                            "reason": (
                                "fail_safe_forced_liquidation_"
                                f"{snapshot.get('last_trigger_reason') or 'active'}"
                            ),
                            "fail_safe_forced": True,
                            "extended_hours_fail_safe": not market_is_open,
                            "extended_hours_context": extended_context,
                        }
                    )
                    break
            continue

        qty = safe_float(position_qty.get(symbol), 0.0)
        price = _price_for_fail_safe_sell(symbol, executable)
        extended_context = None
        if not market_is_open:
            extended_context = _fail_safe_extended_quote(symbol)
            if not extended_context.get("ok"):
                logging.warning(
                    "[Layer5Exec] Fail-safe extended-hours row deferred | "
                    "symbol=%s reason=%s context=%s",
                    symbol,
                    extended_context.get("reason"),
                    extended_context,
                )
                continue
            price = safe_float(extended_context.get("limit_price"), 0.0)

        if qty <= 0 or price <= 0:
            logging.warning(
                "[Layer5Exec] Fail-safe liquidation row skipped; missing qty/price | "
                "symbol=%s qty=%s price=%s cycle_id=%s plan_id=%s",
                symbol,
                qty,
                price,
                cycle_id,
                plan_id,
            )
            continue

        row = {
            "row_id": f"{plan_id or 'layer5'}:FAILSAFE:{symbol}",
            "cycle_id": cycle_id,
            "plan_id": plan_id,
            "symbol": symbol,
            "decision": "SELL",
            "qty": qty,
            "price": price,
            "notional": qty * price,
            "reason": f"fail_safe_forced_liquidation_{snapshot.get('last_trigger_reason') or 'active'}",
            "fail_safe_forced": True,
            "extended_hours_fail_safe": not market_is_open,
            "extended_hours_context": extended_context,
        }

        executable.append(row)
        added_rows.append(row)
        sell_symbols.add(symbol)

    if added_rows:
        logging.warning(
            "[Layer5Exec] Added fail-safe liquidation SELL rows | cycle_id=%s "
            "plan_id=%s rows=%s",
            cycle_id,
            plan_id,
            [compact_executable_row_for_log(row) for row in added_rows],
        )

    executable.sort(
        key=lambda r: (
            0 if _normalize_decision(r.get("decision")) == "SELL" else 1,
            -abs(safe_float(r.get("notional"), 0.0)),
        )
    )

    return executable


def _record_fail_safe_blocked_buys(
    *,
    result: dict,
    blocked_rows: list[dict],
    cycle_id: Any,
    plan_id: Any,
) -> None:
    for row in blocked_rows:
        qty = safe_float(row.get("qty"), 0.0)
        price = safe_float(row.get("price"), 0.0)
        notional = safe_float(row.get("notional"), qty * price)
        row_id = row.get("row_id") or f"{plan_id}:{row.get('symbol')}"

        result["attempted"] += 1
        result["skipped"] += 1
        result["orders"].append(
            {
                "symbol": row.get("symbol"),
                "side": "buy",
                "status": "skipped",
                "reason": row.get("_fail_safe_block_reason")
                or "fail_safe_active_blocks_buy",
                "qty": qty,
                "notional": notional,
                "price": price,
                "plan_id": plan_id,
                "row_id": row_id,
            }
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

    append_layer4_order_rows(
        result
    )

    try:
        register_layer5_cycle_submissions(
            result
        )
    except Exception:
        logging.warning(
            "[Layer5Exec] Failed registering "
            "cycle submission diagnostics | "
            "cycle_id=%s plan_id=%s",
            cycle_id,
            plan_id,
            exc_info=True,
        )

    return result


def _execute_layer5_plan_unlocked(plan: Any, summary: dict | None = None) -> dict:
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
        "strategy_execution_blocked_reason": None,
        "strategy_rows_blocked": 0,
        "execution_sequence": "sells_then_cash_refresh_then_buys",
        "sell_phase_wait": None,
        "cash_after_sell_refresh": None,
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

    fail_safe_snapshot = _fail_safe_snapshot()
    layer5_state["last_fail_safe_snapshot"] = fail_safe_snapshot

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

    extended_fail_safe_allowed, extended_session = (
        _fail_safe_after_hours_window()
    )
    fail_safe_market_exception = (
        not market_is_open
        and bool(fail_safe_snapshot.get("active"))
        and extended_fail_safe_allowed
    )
    if (
        _market_hours_only()
        and not market_is_open
        and not fail_safe_market_exception
    ):
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
    if fail_safe_market_exception:
        result["fail_safe_market_hours_exception"] = True
        result["fail_safe_extended_session"] = extended_session
        logging.warning(
            "[Layer5Exec] Allowing fail-safe-only extended-hours execution | "
            "cycle_id=%s plan_id=%s session=%s",
            cycle_id,
            plan_id,
            extended_session,
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

    if open_orders and not fail_safe_snapshot["active"]:
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
    elif open_orders:
        logging.warning(
            "[Layer5Exec] Existing broker orders detected while fail-safe is active; "
            "continuing with per-symbol duplicate checks. open_order_count=%s",
            len(open_orders),
        )

    position_qty = _position_qty_by_symbol(client)
    _refresh_broker_snapshot(client, layer5_state, label="pre_execution")
    available_cash_budget = _available_cash(client)

    executable = _executable_rows(plan)

    strategy_execution_blocked_reason = str(
        summary.get(
            "strategy_execution_blocked_reason"
        )
        or ""
    ).strip()

    if strategy_execution_blocked_reason:
        result[
            "strategy_execution_blocked_reason"
        ] = strategy_execution_blocked_reason

        result[
            "strategy_rows_blocked"
        ] = len(executable)

        if fail_safe_snapshot["active"]:
            logging.warning(
                "[Layer5Exec] Ordinary strategy rows blocked but fail-safe "
                "liquidation remains enabled | cycle_id=%s plan_id=%s "
                "reason=%s blocked_rows=%s",
                cycle_id,
                plan_id,
                strategy_execution_blocked_reason,
                len(executable),
            )

            # Remove all ordinary BUY and SELL rows. The fail-safe branch below
            # may still append explicit liquidation rows from fail-safe state.
            executable = []

        else:
            logging.warning(
                "[Layer5Exec] Ordinary strategy execution blocked | "
                "cycle_id=%s plan_id=%s reason=%s blocked_rows=%s",
                cycle_id,
                plan_id,
                strategy_execution_blocked_reason,
                len(executable),
            )

            result["blocked_reason"] = (
                strategy_execution_blocked_reason
            )

            result["count_integrity_ok"] = True

            return _finish_layer5_result(
                result=result,
                layer5_state=layer5_state,
                started_monotonic=started_monotonic,
            )

    reentry_blocked_symbols = set(
        fail_safe_snapshot.get("reentry_blocked_symbols") or []
    )
    if reentry_blocked_symbols:
        cooldown_rows = [
            row for row in executable
            if row.get("decision") == "BUY"
            and row.get("symbol") in reentry_blocked_symbols
        ]
        for row in cooldown_rows:
            row["_fail_safe_block_reason"] = (
                "fail_safe_reentry_cooldown_blocks_buy"
            )
        cooldown_ids = {id(row) for row in cooldown_rows}
        executable = [
            row for row in executable if id(row) not in cooldown_ids
        ]
        _record_fail_safe_blocked_buys(
            result=result,
            blocked_rows=cooldown_rows,
            cycle_id=cycle_id,
            plan_id=plan_id,
        )

    if fail_safe_snapshot["active"]:
        original_executable_count = len(executable)
        active_symbols = set(fail_safe_snapshot.get("symbols") or [])
        blocked_buy_rows = [row for row in executable if row.get("decision") == "BUY" and (fail_safe_snapshot.get("global_active") or row.get("symbol") in active_symbols)]
        for row in blocked_buy_rows:
            row["_fail_safe_block_reason"] = "fail_safe_active_blocks_buy"
        blocked_ids = {id(row) for row in blocked_buy_rows}
        executable = [row for row in executable if id(row) not in blocked_ids]

        _record_fail_safe_blocked_buys(
            result=result,
            blocked_rows=blocked_buy_rows,
            cycle_id=cycle_id,
            plan_id=plan_id,
        )

        executable = _append_fail_safe_liquidation_rows(
            executable,
            snapshot=fail_safe_snapshot,
            position_qty=position_qty,
            cycle_id=cycle_id,
            plan_id=plan_id,
            market_is_open=market_is_open,
        )

        logging.warning(
            "[Layer5Exec] Fail-safe active; SELL-only execution policy applied | "
            "cycle_id=%s plan_id=%s event_set=%s state=%s reason=%s symbol=%s "
            "pending=%s liquidate_all=%s original_executable=%s blocked_buys=%s sell_rows=%s",
            cycle_id,
            plan_id,
            fail_safe_snapshot.get("event_set"),
            fail_safe_snapshot.get("state"),
            fail_safe_snapshot.get("last_trigger_reason"),
            fail_safe_snapshot.get("symbol"),
            fail_safe_snapshot.get("pending_liquidation_symbols"),
            fail_safe_snapshot.get("liquidate_all"),
            original_executable_count,
            len(blocked_buy_rows),
            len(executable),
        )

    if not executable:
        logging.info(
            "[Layer5Exec] No executable SELL rows while fail-safe active. cycle_id=%s plan_id=%s"
            if fail_safe_snapshot["active"]
            else "[Layer5Exec] No executable BUY/SELL rows. cycle_id=%s plan_id=%s",
            cycle_id,
            plan_id,
        )
        result["blocked_reason"] = (
            "fail_safe_active_no_sell_rows"
            if fail_safe_snapshot["active"]
            else "fail_safe_reentry_cooldown_no_rows"
            if reentry_blocked_symbols and result["skipped"]
            else "no_executable_rows"
        )
        return _finish_layer5_result(
            result=result,
            layer5_state=layer5_state,
            started_monotonic=started_monotonic,
            log_level=logging.WARNING if fail_safe_snapshot["active"] else logging.INFO,
        )

    logging.info(
        "[Layer5Exec] Executable rows found | cycle_id=%s plan_id=%s count=%s rows=%s",
        cycle_id,
        plan_id,
        len(executable),
        [compact_executable_row_for_log(row) for row in executable],
    )

    executable = sorted(
        executable,
        key=lambda row: (
            0 if row.get("decision") == "SELL" else 1,
            str(row.get("symbol") or ""),
        ),
    )
    submitted_sell_order_ids: list[str] = []
    buy_phase_started = False

    for row in executable:
        symbol = row["symbol"]
        decision = row["decision"]
        qty = safe_float(row["qty"], 0.0)
        price = safe_float(row["price"], 0.0)
        notional = safe_float(row.get("notional"), qty * price)
        reason = str(row.get("reason", ""))
        row_id = row.get("row_id") or f"{plan_id}:{symbol}"

        if decision == "BUY" and not buy_phase_started:
            buy_phase_started = True
            wait_timeout = safe_float(
                app_state.get("execution", {}).get(
                    "sell_first_wait_timeout_seconds",
                    12.0,
                ),
                12.0,
            )
            result["sell_phase_wait"] = _wait_for_sell_terminal_states(
                client,
                submitted_sell_order_ids,
                timeout_seconds=wait_timeout,
            )
            refreshed = _refresh_broker_snapshot(
                client,
                layer5_state,
                label="after_sell_phase",
            )
            if refreshed.get("positions_ok"):
                position_qty = {
                    broker_symbol: safe_float(position.get("qty"), 0.0)
                    for broker_symbol, position in refreshed.get(
                        "positions",
                        {},
                    ).items()
                }
            if refreshed.get("account_ok"):
                available_cash_budget = safe_float(
                    refreshed.get("cash"),
                    available_cash_budget,
                )
            result["cash_after_sell_refresh"] = available_cash_budget
            logging.info(
                "[Layer5Exec] Sell phase complete; refreshed broker state "
                "before buys | cycle_id=%s plan_id=%s sells=%s "
                "terminal=%s timed_out=%s cash=%s",
                cycle_id,
                plan_id,
                len(submitted_sell_order_ids),
                result["sell_phase_wait"].get("terminal_order_count"),
                result["sell_phase_wait"].get("timed_out_order_ids"),
                available_cash_budget,
            )

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

        if open_order_count_for_symbol > 0:
            result["skipped"] += 1
            result["orders"].append(
                {
                    "symbol": symbol,
                    "side": decision.lower(),
                    "status": "skipped",
                    "reason": "existing_broker_order_for_symbol",
                    "open_order_count_for_symbol": open_order_count_for_symbol,
                    "plan_id": plan_id,
                    "row_id": row_id,
                }
            )
            continue

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

        computed_side = normalize_side(
            side
        )

        order_request = None
        order_request_side = None
        order_type = None
        order_time_in_force = None

        cash_budget_after = (
            available_cash_budget
        )

        submission_context = (
            _submission_price_context(
                symbol,
                price,
            )
        )

        broker_submit_started_monotonic = (
            None
        )
        fail_safe_lifecycle_id = None

        try:
            if row.get("fail_safe_forced") and not mark_submission_started(symbol):
                result["skipped"] += 1
                result["orders"].append(
                    {
                        "symbol": symbol,
                        "side": "sell",
                        "status": "skipped",
                        "reason": "fail_safe_not_submission_eligible",
                        "plan_id": plan_id,
                        "row_id": row_id,
                    }
                )
                continue
            if row.get("fail_safe_forced"):
                fail_safe_lifecycle_id = (
                    fail_safe_lifecycle_snapshot()
                    .get("lifecycles", {})
                    .get(symbol, {})
                    .get("lifecycle_id")
                )

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

            broker_submit_started_at = (
                datetime.now(
                    timezone.utc
                )
            )

            broker_submit_started_monotonic = (
                time.monotonic()
            )

            submission_context[
                "broker_submit_started_at"
            ] = (
                broker_submit_started_at.isoformat()
            )

            submitted_order = (
                client.submit_order(
                    order_request
                )
            )

            broker_submit_completed_at = (
                datetime.now(
                    timezone.utc
                )
            )

            submission_context.update({
                "broker_submit_completed_at": (
                    broker_submit_completed_at.isoformat()
                ),
                "broker_submit_latency_ms": round(
                    (
                        time.monotonic()
                        - broker_submit_started_monotonic
                    )
                    * 1000.0,
                    3,
                ),
                "broker_status_at_submit": (
                    _serialize_enum_or_value(
                        getattr(
                            submitted_order,
                            "status",
                            None,
                        )
                    )
                ),
                "broker_created_at": (
                    _serialize_datetime_or_value(
                        getattr(
                            submitted_order,
                            "created_at",
                            None,
                        )
                    )
                ),
                "broker_submitted_at": (
                    _serialize_datetime_or_value(
                        getattr(
                            submitted_order,
                            "submitted_at",
                            None,
                        )
                    )
                ),
            })

            broker_limit_price = safe_float(
                getattr(
                    submitted_order,
                    "limit_price",
                    0.0,
                ),
                0.0,
            )

            submission_context[
                "broker_limit_price"
            ] = (
                round(
                    broker_limit_price,
                    6,
                )
                if broker_limit_price > 0
                else None
            )

            order_id = getattr(
                submitted_order,
                "id",
                None,
            )

            if not order_id:
                raise RuntimeError(f"Broker did not return order id for {symbol}")

            execution_phase = (
                "sell"
                if decision == "SELL"
                else "buy_after_sell_refresh"
            )
            trade_attribution, trade_attribution_evidence = (
                _trade_attribution(
                    row,
                    broker_position_qty=position_qty_before,
                )
            )
            trade_attribution_detail = _trade_attribution_detail(row)
            if decision == "SELL":
                submitted_sell_order_ids.append(str(order_id))

            track_limit_order(
                symbol=symbol,
                order_id=order_id,
                side=side,
                qty=qty,
                limit_price=(
                    price
                    if not market_is_open
                    else None
                ),
                market_is_open=market_is_open,
                submission_context={
                    **submission_context,
                    "cycle_id": cycle_id,
                    "plan_id": plan_id,
                    "row_id": row_id,
                    "reason": reason,
                    "planned_notional": (
                        notional
                    ),
                    "execution_phase": execution_phase,
                    "trade_attribution": trade_attribution,
                    "trade_attribution_evidence": trade_attribution_evidence,
                    "trade_attribution_detail": trade_attribution_detail,
                    "extended_hours_fail_safe": row.get(
                        "extended_hours_fail_safe"
                    ),
                    "extended_hours_context": row.get(
                        "extended_hours_context"
                    ),
                    "extended_hours_reprice_seconds": _config_value(
                        "FAIL_SAFE_EXTENDED_HOURS_REPRICE_SECONDS",
                        20.0,
                    ),
                    "fail_safe_lifecycle_id": fail_safe_lifecycle_id,
                },
            )

            if normalize_side(side) == "buy":
                open_trades = app_state.setdefault("open_trades", {})
                existing = open_trades.get(symbol)
                if (
                    isinstance(existing, dict)
                    and position_qty_before > 0
                ):
                    existing["pending_buy_order_id"] = str(order_id)
                    existing["pending_buy_quantity"] = qty
                    existing["broker_quantity_before_order"] = (
                        position_qty_before
                    )
                    existing["source"] = "layer5"
                    existing["layer5_reason"] = reason
                    existing["cycle_id"] = cycle_id
                    existing["plan_id"] = plan_id
                    existing["row_id"] = row_id
                else:
                    open_trades[symbol] = {
                        "status": "pending",
                        "order_id": str(order_id),
                        "quantity": 0.0,
                        "broker_quantity_before_order": (
                            position_qty_before
                        ),
                        "pending_buy_quantity": qty,
                        "buy_price": price,
                        "buy_time": datetime.now(timezone.utc),
                        "source": "layer5",
                        "layer4_reason": reason,
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
                existing["pending_sell_order_id"] = str(order_id)
                existing["sell_quantity"] = qty
                existing["broker_quantity_before_order"] = (
                    position_qty_before
                )
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

            if decision == "SELL" and row.get("fail_safe_forced"):
                mark_submitted(symbol, submitted_order)

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
                    "execution_phase": execution_phase,
                    "trade_attribution": trade_attribution,
                    "trade_attribution_evidence": trade_attribution_evidence,
                    "trade_attribution_detail": trade_attribution_detail,
                    "extended_hours_fail_safe": row.get(
                        "extended_hours_fail_safe"
                    ),
                    "extended_hours_context": row.get(
                        "extended_hours_context"
                    ),
                    "order_id": str(order_id),
                    "qty": qty,
                    "notional": notional,
                    "price": price,
                    "reason": reason,
                    "plan_id": plan_id,
                    "row_id": row_id,

                    **submission_context,
                }
            )

        except Exception as exc:
            if row.get("fail_safe_forced"):
                mark_submission_failed(symbol, exc)

            if (
                broker_submit_started_monotonic
                is not None
                and submission_context.get(
                    "broker_submit_completed_at"
                )
                is None
            ):
                broker_submit_completed_at = (
                    datetime.now(
                        timezone.utc
                    )
                )

                submission_context[
                    "broker_submit_completed_at"
                ] = (
                    broker_submit_completed_at.isoformat()
                )

                submission_context[
                    "broker_submit_latency_ms"
                ] = round(
                    (
                        time.monotonic()
                        - broker_submit_started_monotonic
                    )
                    * 1000.0,
                    3,
                )

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

                    **submission_context,
                }
            )

    _refresh_broker_snapshot(client, layer5_state, label="post_execution")

    return _finish_layer5_result(
        result=result,
        layer5_state=layer5_state,
        started_monotonic=started_monotonic,
        log_level=logging.WARNING if result["errors"] else logging.INFO,
    )

def execute_layer5_plan(plan: Any, summary: dict | None = None) -> dict:
    """Serialize strategic and immediate fail-safe submission passes."""
    with _LAYER5_EXECUTION_LOCK:
        return _execute_layer5_plan_unlocked(plan, summary)
def _trade_attribution(
    row: dict,
    *,
    broker_position_qty: float | None = None,
) -> tuple[str, str]:
    """Classify why an executable row exists without affecting execution."""
    reason = str(row.get("reason") or "").lower()
    planned_current_qty = row.get("current_qty")
    if planned_current_qty is not None and broker_position_qty is not None:
        planned_qty = safe_float(planned_current_qty, 0.0)
        broker_qty = safe_float(broker_position_qty, 0.0)
        if abs(planned_qty - broker_qty) > 0.000001:
            return (
                "stale_local_state_correction",
                (
                    "Layer 3 current_qty differed from broker position at "
                    f"execution ({planned_qty} vs {broker_qty})"
                ),
            )
    if row.get("fail_safe_forced") or any(
        token in reason for token in ("fail_safe", "risk", "loss")
    ):
        return "risk_or_fail_safe", "risk/fail-safe reason"
    if any(
        token in reason
        for token in (
            "reconcile",
            "broker_state",
            "stale_local",
            "position_mismatch",
        )
    ):
        category = (
            "stale_local_state_correction"
            if "stale" in reason or "mismatch" in reason
            else "broker_state_reconciliation"
        )
        return category, "state-reconciliation reason"
    if row.get("target_hysteresis_changed_target") or any(
        token in reason
        for token in ("ranking", "rank_change", "target_added", "target_removed")
    ):
        return "genuine_ranking_change", "target/ranking-change evidence"
    if any(
        row.get(key) is not None
        for key in (
            "target_weight",
            "target_qty",
            "qty_delta",
            "delta_weight",
        )
    ):
        return "target_size_change", "target-size delta evidence"
    return "unknown", "insufficient attribution evidence"


def _trade_attribution_detail(row: dict) -> str:
    reason = str(row.get("reason") or "").lower()
    decision = _normalize_decision(row.get("decision"))
    current_qty = safe_float(row.get("current_qty"), 0.0)
    target_weight = safe_float(row.get("target_weight"), 0.0)
    if row.get("fail_safe_forced") or "fail_safe" in reason:
        return "forced_risk_liquidation"
    if any(token in reason for token in ("stale", "mismatch", "reconcile")):
        return "state_reconciliation"
    if decision == "BUY" and (
        current_qty <= 0
        or "target_added" in reason
        or "new_target" in reason
    ):
        return "ranked_set_entry"
    if decision == "SELL" and (
        target_weight <= 0
        or "target_removed" in reason
        or "removal" in reason
    ):
        return "ranked_set_exit"
    if row.get("target_hysteresis_changed_target"):
        return "hysteresis_confirmed_target_change"
    if any(token in reason for token in ("rank", "score")):
        return "rank_or_score_weight_change"
    return "target_resize"
