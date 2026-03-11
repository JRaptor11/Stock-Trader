import logging
import asyncio
from datetime import datetime, timezone
from alpaca.trading.enums import TimeInForce, QueryOrderStatus, OrderSide
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest
from utils.threading_utils import safe_thread
from state import app_state

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


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def track_limit_order(symbol, order_id, side=None, qty=None, limit_price=None, market_is_open=None):
    """
    Track a submitted order with enough metadata to support duplicate blocking
    and extended-hours cancel/replace decisions.
    """
    app_state.setdefault("open_orders", {})[symbol] = {
        "order_id": str(order_id),
        "side": normalize_side(side) if side is not None else None,
        "qty": _safe_float(qty, 0),
        "limit_price": _safe_float(limit_price, 0),
        "market_is_open": bool(market_is_open) if market_is_open is not None else None,
        "tracked_at": datetime.now(timezone.utc).isoformat(),
    }

    logging.info(
        f"[TRACK] 🛰️ Tracking order for {symbol}: "
        f"id={order_id}, side={side}, qty={qty}, limit_price={limit_price}, market_is_open={market_is_open}"
    )

    if not app_state.get("monitoring_orders", False):
        app_state["monitoring_orders"] = True
        safe_thread(_order_monitor_thread_entry, name="OrderMonitor", daemon=True)


def get_tracked_order(symbol: str) -> dict | None:
    entry = app_state.setdefault("open_orders", {}).get(symbol)
    return entry if isinstance(entry, dict) else None


def clear_tracked_order(symbol: str) -> None:
    app_state.setdefault("open_orders", {}).pop(symbol, None)


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
        if trade_info and trade_info.get("status") in {"pending", "filled", "pending_sell"}:
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
                        del app_state["open_orders"][symbol]
                        continue

                    order = client.get_order_by_id(order_id)
                    status = str(getattr(order, "status", "")).lower()
                    logging.debug(f"[OrderMonitor] {symbol} → {status}")

                    if status == "filled":
                        qty = float(getattr(order, "filled_qty", 0) or 0)
                        price = float(getattr(order, "filled_avg_price", 0) or 0)

                        if tracked_side == "buy":
                            app_state["open_trades"][symbol] = {
                                "buy_price": price,
                                "buy_time": datetime.now(timezone.utc),
                                "quantity": qty,
                                "status": "filled",
                                "order_id": order_id,
                            }
                            logging.info(f"[✓ Filled BUY] {symbol}: {qty} @ ${price:.2f}")

                        elif tracked_side == "sell":
                            app_state.setdefault("open_trades", {}).pop(symbol, None)
                            app_state["strategy"].setdefault("sells_in_progress", set()).discard(symbol)
                            logging.info(f"[✓ Filled SELL] {symbol}: {qty} @ ${price:.2f}")

                        else:
                            # fallback if older tracked format exists
                            app_state["open_trades"][symbol] = {
                                "buy_price": price,
                                "buy_time": datetime.now(timezone.utc),
                                "quantity": qty,
                                "status": "filled",
                                "order_id": order_id,
                            }
                            logging.info(f"[✓ Filled] {symbol}: {qty} @ ${price:.2f}")

                        del app_state["open_orders"][symbol]

                    elif status in ("canceled", "expired", "rejected", "done_for_day"):
                        if tracked_side == "sell":
                            app_state["strategy"].setdefault("sells_in_progress", set()).discard(symbol)

                        trade_info = app_state.get("open_trades", {}).get(symbol)
                        if tracked_side == "buy" and trade_info and trade_info.get("status") == "pending":
                            app_state["open_trades"].pop(symbol, None)

                        del app_state["open_orders"][symbol]
                        logging.warning(f"[✖️ OrderClosed] {symbol} → {status.upper()} — removed from tracking")

                    else:
                        continue

                except Exception as e:
                    logging.warning(f"[⚠️ OrderMonitor] {symbol} → Order check failed: {e}")

            # interruptible sleep (checks shutdown_event every ~0.25s)
            total = 5.0
            step = 0.25
            end = asyncio.get_event_loop().time() + total
            while asyncio.get_event_loop().time() < end:
                if shutdown_event and shutdown_event.is_set():
                    logging.info("[OrderMonitor] Exiting due to shutdown_event.")
                    return
                await asyncio.sleep(min(step, end - asyncio.get_event_loop().time()))

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
        # === Check local open_trades ===
        trade_info = app_state.get("open_trades", {}).get(symbol)
        logging.debug(f"[PositionStatus] open_trades for {symbol}: {trade_info}")
        
        if trade_info:
            try:
                open_orders = app_state["trading_client"].get_orders()
                logging.debug(f"[PositionStatus] open_orders: {[f'{o.symbol}:{o.side}' for o in open_orders]}")
                has_pending_sell = any(o.symbol == symbol and o.side == "sell" for o in open_orders)
                logging.debug(f"[PositionStatus] {symbol}: has_pending_sell={has_pending_sell}")
                return True, not has_pending_sell
            except Exception as e:
                logging.warning(f"⚠️ Error checking open orders for {symbol}: {e}")
                return True, False

        # === Check Alpaca live positions ===
        positions = app_state["trading_client"].get_all_positions()
        logging.debug(f"[PositionStatus] Alpaca positions: {[p.symbol for p in positions]}")
        
        has_position = any(pos.symbol == symbol and float(pos.qty) > 0 for pos in positions)

        if has_position:
            try:
                params = GetOrdersRequest(status=QueryOrderStatus.OPEN)
                open_orders = app_state["trading_client"].get_orders(filter=params)
                logging.debug(f"[PositionStatus] open_orders: {[f'{o.symbol}:{o.side}' for o in open_orders]}")
                has_pending_sell = any(o.symbol == symbol and o.side == "sell" for o in open_orders)
                logging.debug(f"[PositionStatus] {symbol}: has_pending_sell={has_pending_sell}")
                return True, not has_pending_sell
            except Exception as e:
                logging.warning(f"⚠️ Error checking open orders for {symbol}: {e}")
                return True, False

        # ✅ No position found anywhere
        return False, True

    except Exception as e:
        logging.warning(f"⚠️ Fallback position check failed: {e}")
        return False, True

def check_local_position(symbol, open_trades) -> tuple[bool, bool]:
    has_position = symbol in open_trades
    can_sell = False

    if has_position:
        try:
            params = GetOrdersRequest(status=QueryOrderStatus.OPEN) 
            open_orders = app_state["trading_client"].get_orders(filter=params)
            has_pending_sell = any(
                o.symbol == symbol and o.side == "sell" for o in open_orders
            )
            can_sell = not has_pending_sell
        except Exception as e:
            logging.warning(f"⚠️ Error checking open orders for {symbol}: {e}")
            can_sell = False  # safest fallback

    return has_position, can_sell

