import asyncio
import logging
import time
from datetime import datetime, timezone

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from core.state import app_state


RECONCILE_PENDING_ORDER_GRACE_SECONDS = 5 * 60


BROKER_SNAPSHOT_TIMEOUT_SECONDS = 15.0

_broker_snapshot_fetch_task: asyncio.Task | None = None


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _norm_symbol(symbol) -> str:
    return str(symbol or "").upper().strip()


def _normalize_side(side) -> str:
    value = getattr(side, "value", side)
    return str(value or "").lower().replace("orderside.", "").strip()


def _normalize_status(status) -> str:
    value = getattr(status, "value", status)
    return str(value or "").lower().replace("orderstatus.", "").strip()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seconds_since(value) -> float | None:
    if value is None:
        return None

    try:
        if isinstance(value, datetime):
            dt = value
        else:
            text = str(value)
            if text.endswith("Z"):
                text = text.replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())

    except Exception:
        return None


def _serialize_account(account) -> dict:
    if not account:
        return {}

    return {
        "equity": _safe_float(getattr(account, "equity", 0.0), 0.0),
        "cash": _safe_float(getattr(account, "cash", 0.0), 0.0),
        "buying_power": _safe_float(getattr(account, "buying_power", 0.0), 0.0),
    }


def _serialize_position(pos) -> dict:
    symbol = _norm_symbol(getattr(pos, "symbol", ""))

    qty = _safe_float(getattr(pos, "qty", 0.0), 0.0)
    avg_entry_price = _safe_float(getattr(pos, "avg_entry_price", 0.0), 0.0)
    current_price = _safe_float(getattr(pos, "current_price", 0.0), 0.0)
    market_value = _safe_float(getattr(pos, "market_value", 0.0), 0.0)
    unrealized_plpc = _safe_float(getattr(pos, "unrealized_plpc", 0.0), 0.0)

    if current_price <= 0 and qty > 0 and market_value > 0:
        current_price = market_value / qty

    if market_value <= 0 and current_price > 0:
        market_value = qty * current_price

    return {
        "symbol": symbol,
        "qty": qty,
        "avg_entry_price": avg_entry_price,
        "current_price": current_price,
        "market_value": market_value,
        "unrealized_plpc": unrealized_plpc,
    }


def _serialize_order(order) -> dict:
    symbol = _norm_symbol(getattr(order, "symbol", ""))

    return {
        "id": str(getattr(order, "id", "")),
        "symbol": symbol,
        "side": _normalize_side(getattr(order, "side", "")),
        "status": _normalize_status(getattr(order, "status", "")),
        "type": str(getattr(order, "type", "")).lower(),
        "qty": _safe_float(getattr(order, "qty", 0.0), 0.0),
        "filled_qty": _safe_float(getattr(order, "filled_qty", 0.0), 0.0),
        "limit_price": _safe_float(getattr(order, "limit_price", 0.0), 0.0),
        "submitted_at": str(getattr(order, "submitted_at", "") or ""),
    }


def _fetch_broker_snapshot_sync(client):
    """
    Fetch one broker snapshot inside a worker thread.

    Keeping the three synchronous Alpaca SDK calls in one worker task avoids
    blocking the asyncio event loop and makes overlapping snapshot prevention
    straightforward.
    """
    account = client.get_account()
    positions = client.get_all_positions()

    params = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    open_orders = client.get_orders(filter=params)

    return account, positions, open_orders


def _finish_broker_snapshot_fetch(task: asyncio.Task) -> None:
    """
    Release the retained broker snapshot task and consume any late exception.
    """
    global _broker_snapshot_fetch_task

    if _broker_snapshot_fetch_task is task:
        _broker_snapshot_fetch_task = None

    try:
        task.result()
    except asyncio.CancelledError:
        logging.debug(
            "[PortfolioReconcile] Broker snapshot task was cancelled."
        )
    except Exception as exc:
        logging.debug(
            "[PortfolioReconcile] Broker snapshot task completed "
            "with error: %s",
            exc,
        )


async def _fetch_broker_snapshot() -> tuple[dict, dict, dict]:
    """
    Fetch Alpaca account, positions, and open orders.

    Alpaca is the source of truth for real portfolio state. The synchronous
    SDK calls run in a retained worker task with a bounded asyncio wait.
    """
    global _broker_snapshot_fetch_task

    client = app_state.get("trading_client")
    if not client:
        raise RuntimeError("missing_trading_client")

    existing_task = _broker_snapshot_fetch_task

    if existing_task is not None and not existing_task.done():
        raise TimeoutError(
            "Previous broker snapshot request is still running; "
            "skipping overlapping reconciliation request."
        )

    fetch_task = asyncio.create_task(
        asyncio.to_thread(
            _fetch_broker_snapshot_sync,
            client,
        ),
        name="portfolio-reconciler-broker-snapshot",
    )

    _broker_snapshot_fetch_task = fetch_task
    fetch_task.add_done_callback(_finish_broker_snapshot_fetch)

    try:
        account, positions, open_orders = await asyncio.wait_for(
            asyncio.shield(fetch_task),
            timeout=BROKER_SNAPSHOT_TIMEOUT_SECONDS,
        )

    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            "Broker snapshot request exceeded "
            f"{BROKER_SNAPSHOT_TIMEOUT_SECONDS:.1f} seconds."
        ) from exc

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        raise RuntimeError(
            f"Broker snapshot request failed: {exc}"
        ) from exc

    account_snapshot = _serialize_account(account)

    positions_by_symbol = {}

    for position in positions:
        row = _serialize_position(position)
        symbol = row["symbol"]

        if symbol and row["qty"] > 0:
            positions_by_symbol[symbol] = row

    open_orders_by_id = {}

    for order in open_orders:
        row = _serialize_order(order)
        order_id = row["id"]

        if order_id:
            open_orders_by_id[order_id] = row

    return (
        account_snapshot,
        positions_by_symbol,
        open_orders_by_id,
    )


def _open_orders_by_symbol(open_orders_by_id: dict) -> dict:
    by_symbol = {}

    for order in open_orders_by_id.values():
        symbol = _norm_symbol(order.get("symbol"))
        if not symbol:
            continue

        by_symbol.setdefault(symbol, []).append(order)

    return by_symbol


def _sync_broker_positions_to_open_trades(
    *,
    broker_positions: dict,
    broker_open_orders_by_symbol: dict,
    repair: bool,
) -> tuple[list[dict], list[dict]]:
    """
    Compare broker positions against local app_state['open_trades'].

    Safe repairs:
    - Broker has a position but local open_trades is missing/stale: add or update as synced.
    - Local says filled/synced/pending_sell but broker has no position: remove local stale position.
    - Quantity mismatch: update local quantity to broker qty.
    """
    mismatches = []
    repairs = []

    open_trades = app_state.setdefault("open_trades", {})
    broker_symbols = set(broker_positions.keys())
    local_symbols = set(open_trades.keys())

    # Broker has position, local missing or wrong.
    for symbol, broker_pos in broker_positions.items():
        local = open_trades.get(symbol)
        broker_qty = _safe_float(broker_pos.get("qty"), 0.0)
        broker_avg = _safe_float(broker_pos.get("avg_entry_price"), 0.0)

        if not isinstance(local, dict):
            mismatches.append({
                "type": "broker_position_missing_locally",
                "symbol": symbol,
                "broker_qty": broker_qty,
            })

            if repair:
                open_trades[symbol] = {
                    "buy_price": broker_avg,
                    "buy_time": datetime.now(timezone.utc),
                    "quantity": broker_qty,
                    "status": "synced",
                    "order_id": None,
                    "max_price": broker_avg,
                    "reconciled_at": _iso_now(),
                    "reconcile_source": "broker_position_missing_locally",
                }

                repairs.append({
                    "type": "added_synced_open_trade",
                    "symbol": symbol,
                    "qty": broker_qty,
                })

            continue

        status = str(local.get("status", "")).lower().strip()
        local_qty = _safe_float(local.get("quantity"), 0.0)

        if status not in {"filled", "pending_sell", "synced"}:
            mismatches.append({
                "type": "broker_position_local_status_not_active",
                "symbol": symbol,
                "local_status": status,
                "broker_qty": broker_qty,
            })

            if repair:
                local["status"] = "synced"
                local["quantity"] = broker_qty
                local["buy_price"] = _safe_float(local.get("buy_price"), broker_avg) or broker_avg
                local["buy_time"] = local.get("buy_time", datetime.now(timezone.utc))
                local["max_price"] = max(
                    _safe_float(local.get("max_price"), broker_avg),
                    broker_avg,
                )
                local["reconciled_at"] = _iso_now()
                local["reconcile_source"] = "broker_position_local_status_not_active"

                repairs.append({
                    "type": "repaired_local_status_to_synced",
                    "symbol": symbol,
                    "qty": broker_qty,
                })

            continue

        if abs(local_qty - broker_qty) > 0.000001:
            mismatches.append({
                "type": "quantity_mismatch",
                "symbol": symbol,
                "local_qty": local_qty,
                "broker_qty": broker_qty,
            })

            if repair:
                local["quantity"] = broker_qty
                local["reconciled_at"] = _iso_now()
                local["reconcile_source"] = "quantity_mismatch"

                # Broker average is useful after partial fills/adds.
                if broker_avg > 0:
                    local["buy_price"] = broker_avg
                    local["max_price"] = max(
                        _safe_float(local.get("max_price"), broker_avg),
                        broker_avg,
                    )

                repairs.append({
                    "type": "updated_local_quantity",
                    "symbol": symbol,
                    "local_qty_old": local_qty,
                    "broker_qty": broker_qty,
                })

    # Local says position exists, broker says no.
    for symbol in sorted(local_symbols - broker_symbols):
        local = open_trades.get(symbol)
        if not isinstance(local, dict):
            continue

        status = str(local.get("status", "")).lower().strip()
        broker_orders_for_symbol = broker_open_orders_by_symbol.get(symbol, [])

        if status in {"filled", "synced", "pending_sell"}:
            mismatches.append({
                "type": "local_position_missing_at_broker",
                "symbol": symbol,
                "local_status": status,
                "broker_open_orders": len(broker_orders_for_symbol),
            })

            if repair:
                open_trades.pop(symbol, None)
                app_state.setdefault("strategy", {}).setdefault("sells_in_progress", set()).discard(symbol)

                repairs.append({
                    "type": "removed_stale_local_position",
                    "symbol": symbol,
                    "old_status": status,
                })

        elif status == "pending":
            # Pending buys can briefly exist before broker/open-order state catches up.
            # Do not immediately remove them unless they are old and no matching broker order exists.
            age = _seconds_since(local.get("buy_time"))
            order_id = str(local.get("order_id", "") or "")
            matching_order_exists = order_id in app_state.get("portfolio_reconcile", {}).get("broker_snapshot", {}).get("open_orders", {})

            if not matching_order_exists and (age is None or age >= RECONCILE_PENDING_ORDER_GRACE_SECONDS):
                mismatches.append({
                    "type": "stale_pending_local_buy_without_broker_order",
                    "symbol": symbol,
                    "age_seconds": age,
                    "order_id": order_id,
                })

                if repair:
                    open_trades.pop(symbol, None)
                    repairs.append({
                        "type": "removed_stale_pending_local_buy",
                        "symbol": symbol,
                        "age_seconds": age,
                    })

    return mismatches, repairs


def _compare_local_open_orders(
    *,
    broker_open_orders_by_id: dict,
    broker_open_orders_by_symbol: dict,
) -> list[dict]:
    """
    Compare local app_state['open_orders'] against broker open orders.

    Version 1 logs mismatches only. The order monitor remains responsible for
    fill/cancel finalization.
    """
    mismatches = []
    local_open_orders = app_state.setdefault("open_orders", {})

    broker_ids = set(broker_open_orders_by_id.keys())

    for symbol, tracked in list(local_open_orders.items()):
        if not isinstance(tracked, dict):
            mismatches.append({
                "type": "local_open_order_legacy_format",
                "symbol": symbol,
            })
            continue

        local_order_id = str(tracked.get("order_id", "") or "")

        if not local_order_id:
            mismatches.append({
                "type": "local_open_order_missing_order_id",
                "symbol": symbol,
            })
            continue

        if local_order_id not in broker_ids:
            mismatches.append({
                "type": "local_open_order_not_broker_open",
                "symbol": symbol,
                "order_id": local_order_id,
                "note": "order_monitor_should_finalize_or_clear_this",
            })

    # Broker has open orders that local state does not know about.
    local_ids = {
        str(v.get("order_id", "") or "")
        for v in local_open_orders.values()
        if isinstance(v, dict)
    }

    for order_id, broker_order in broker_open_orders_by_id.items():
        if order_id not in local_ids:
            mismatches.append({
                "type": "broker_open_order_missing_locally",
                "symbol": broker_order.get("symbol"),
                "side": broker_order.get("side"),
                "order_id": order_id,
                "status": broker_order.get("status"),
                "note": "log_only_for_now",
            })

    return mismatches


async def reconcile_portfolio_once(repair: bool = True) -> dict:
    """
    Run one broker-vs-local reconciliation pass.

    Broker truth:
    - account equity/cash
    - positions
    - open broker orders

    Local truth:
    - bot intent metadata
    - open_trades annotations
    - open_orders metadata
    """
    state = app_state.setdefault("portfolio_reconcile", {})
    started = time.time()

    try:
        account_snapshot, broker_positions, broker_open_orders = await _fetch_broker_snapshot()
        broker_open_orders_by_symbol = _open_orders_by_symbol(broker_open_orders)

        state["broker_snapshot"] = {
            "timestamp": _iso_now(),
            "account": account_snapshot,
            "positions": broker_positions,
            "open_orders": broker_open_orders,
        }

        position_mismatches, repairs = _sync_broker_positions_to_open_trades(
            broker_positions=broker_positions,
            broker_open_orders_by_symbol=broker_open_orders_by_symbol,
            repair=repair,
        )

        order_mismatches = _compare_local_open_orders(
            broker_open_orders_by_id=broker_open_orders,
            broker_open_orders_by_symbol=broker_open_orders_by_symbol,
        )

        mismatches = position_mismatches + order_mismatches

        summary = {
            "status": "ok",
            "timestamp": _iso_now(),
            "repair_enabled": bool(repair),
            "broker_position_count": len(broker_positions),
            "local_open_trade_count": len(app_state.setdefault("open_trades", {})),
            "broker_open_order_count": len(broker_open_orders),
            "local_open_order_count": len(app_state.setdefault("open_orders", {})),
            "mismatch_count": len(mismatches),
            "repair_count": len(repairs),
            "duration_seconds": round(time.time() - started, 3),
        }

        state["last_run_at"] = summary["timestamp"]
        state["last_summary"] = summary
        state["last_mismatches"] = mismatches
        state["last_repairs"] = repairs
        state["last_error"] = None

        if mismatches or repairs:
            logging.warning(
                "[PortfolioReconcile] summary=%s mismatches=%s repairs=%s",
                summary,
                mismatches,
                repairs,
            )
        else:
            logging.info("[PortfolioReconcile] Clean snapshot | %s", summary)

        return summary

    except Exception as e:
        summary = {
            "status": "error",
            "timestamp": _iso_now(),
            "error": str(e),
            "duration_seconds": round(time.time() - started, 3),
        }

        state["last_run_at"] = summary["timestamp"]
        state["last_summary"] = summary
        state["last_error"] = str(e)

        logging.warning("[PortfolioReconcile] Failed reconciliation pass: %s", e, exc_info=True)
        return summary


async def run_portfolio_reconciler(interval_seconds: int = 60, repair: bool = True) -> None:
    """
    Background reconciliation loop.

    Runs until shutdown_event is set or task is cancelled.
    """
    state = app_state.setdefault("portfolio_reconcile", {})
    state["running"] = True
    state.setdefault("broker_snapshot", {})
    state.setdefault("last_mismatches", [])
    state.setdefault("last_repairs", [])
    state.setdefault("last_summary", {})

    logging.info(
        "[PortfolioReconcile] Starting portfolio reconciliation loop | interval=%ss repair=%s",
        interval_seconds,
        repair,
    )

    try:
        while not app_state["stream"]["shutdown_event"].is_set():
            await reconcile_portfolio_once(repair=repair)

            end = time.time() + float(interval_seconds)

            while time.time() < end:
                if app_state["stream"]["shutdown_event"].is_set():
                    return
                await asyncio.sleep(min(0.5, end - time.time()))

    except asyncio.CancelledError:
        logging.info("[PortfolioReconcile] Task cancelled.")
        raise

    finally:
        state["running"] = False
        logging.info("[PortfolioReconcile] Reconciliation loop stopped.")