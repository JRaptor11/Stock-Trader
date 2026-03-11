import threading
import asyncio
import time
import signal
import logging
from datetime import datetime, timezone
import csv

from alpaca.trading.enums import OrderSide, OrderType, TimeInForce, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest
from heartbeat import Heartbeat
from state import app_state, active_handle_trade
from strategy import strategy_manager
from utils.alerts_utils import send_email_alert
from utils.trade_utils import log_trade_to_summary, handle_trade_update
from utils.threading_utils import safe_thread
# from utils.lifecycle_utils import record_program_shutdown

from utils.orders_utils import (
    track_limit_order,
    clear_tracked_order,
    create_order_request,
    check_position_status,
    check_local_position,
    reconcile_existing_order,
    normalize_side,
)
from utils import config_utils
from patched_stream import PatchedStockDataStream

import concurrent.futures

# Save original thread constructor for patching elsewhere if desired
_original_thread = threading.Thread

class FakeTrade:
    """ Helper for generating test trade objects """
    def __init__(self, price, symbol):
        self.price = float(f"{price:.2f}")
        self.size = 100
        self.symbol = symbol

    def __repr__(self):
        return f"<FakeTrade symbol={self.symbol} price={self.price} size={self.size}>"

class ThreadedAlpacaStream:
    def __init__(self, api_key, secret_key, symbols):
        self.api_key = api_key
        self.secret_key = secret_key
        self.symbols = symbols
        self.app_state = app_state

        self._manage_task = None

        # self._stream = app_state["stream"]["instance"]
        # self._loop = app_state["stream"]["loop"]
        # self._thread = app_state["stream"]["thread"]
        self._shutdown_event = app_state["stream"]["shutdown_event"]
        self._connection_lock = app_state["stream"]["lock"]
        # self._running = app_state["stream"]["running"]

        self._stream = None
        self._loop = None
        self._thread = None
        self._running = False

        self.heartbeat = Heartbeat()
        self._last_trade_handled = None

        # Public debug snapshot for FastAPI or external tools
        debug = app_state["stream"].setdefault("debug", {})
        debug["status"] = "init"
        debug["last_restart"] = datetime.now(timezone.utc).isoformat()
        debug.setdefault("last_trade", None)

    def _update_debug_snapshot(self, key, value):
        app_state["stream"]["debug"][key] = value

    async def _get_live_stream(self):
        try:
            return PatchedStockDataStream(self.api_key, self.secret_key)
        except Exception as e:
            logging.error(f"Stream init failed: {e}")
            return None

    async def _manage_stream(self):
        reconnect_delay = 5

        connections = app_state["stream"].setdefault("connections", {
            "total_attempts": 0,
            "successful": 0,
            "failed": 0,
            "last_success": None,
            "last_failure": None
        })

        try:
            while not self._shutdown_event.is_set():
                old_stream = None

                # Detach old stream under lock, but do not await under lock
                with self._connection_lock:
                    if self._stream:
                        old_stream = self._stream
                        self._stream = None
                        app_state["stream"]["instance"] = None

                # Stop old stream outside the lock
                if old_stream:
                    try:
                        await old_stream.stop()
                    except Exception as e:
                        logging.warning(f"Stream stop error: {e}")

                if self._shutdown_event.is_set():
                    break

                connections["total_attempts"] += 1
                logging.info(f"🔄 Stream connection attempt #{connections['total_attempts']}")

                new_stream = await self._get_live_stream()

                if self._shutdown_event.is_set():
                    if new_stream:
                        try:
                            await new_stream.stop()
                        except Exception:
                            pass
                    break

                if not new_stream:
                    logging.warning("Failed to initialize stream, retrying...")
                    connections["failed"] += 1
                    connections["last_failure"] = datetime.now(timezone.utc).isoformat()

                    for _ in range(int(reconnect_delay * 10)):
                        if self._shutdown_event.is_set():
                            break
                        await asyncio.sleep(0.1)

                    reconnect_delay = min(reconnect_delay * 2, 60)
                    continue

                with self._connection_lock:
                    self._stream = new_stream
                    app_state["stream"]["instance"] = new_stream

                try:
                    for symbol in self.symbols:
                        self._stream.subscribe_trades(self._handle_trade, symbol)

                    logging.info(f"✅ Subscribed to trades for: {self.symbols}")
                    self.heartbeat.beat()
                    reconnect_delay = 5

                    self._update_debug_snapshot("status", "running")
                    self._update_debug_snapshot("last_restart", datetime.now(timezone.utc).isoformat())

                    connections["successful"] += 1
                    connections["last_success"] = datetime.now(timezone.utc).isoformat()

                    self._running = True
                    app_state["stream"]["running"] = True
                    app_state["stream"]["state"] = "running"

                    await self._stream._run_forever()

                except asyncio.CancelledError:
                    logging.info("Stream stopped normally.")
                    raise
                except Exception as e:
                    logging.error(f"Stream error: {e}")

                    for _ in range(50):  # ~5 seconds total
                        if self._shutdown_event.is_set():
                            break
                        await asyncio.sleep(0.1)

        finally:
            with self._connection_lock:
                self._running = False
                app_state["stream"]["running"] = False
                app_state["stream"]["state"] = "stopped"
                self._stream = None
                app_state["stream"]["instance"] = None

            logging.info("Exiting stream management loop cleanly.")
            self._update_debug_snapshot("status", "stopped")
        
    async def _handle_trade(self, trade):
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        now = time.time()

        if active_handle_trade.get():
            logging.warning(f"[{timestamp}] 🔁 Recursive call to handle_trade blocked.")
            return

        token = active_handle_trade.set(True)
        task = asyncio.current_task()
        app_state["main"]["async_tasks"].add(task)

        try:
            if not hasattr(trade, "price") or not isinstance(trade.price, (float, int)):
                logging.error(f"[{timestamp}] ⚠️ Invalid trade object: {vars(trade)}")
                return

            price_raw = trade.price
            symbol = trade.symbol
            volume = trade.size

            # === 📈 Price/Time Tracking for VolatilityScorer
            # === 📈 Market data tracking (per-symbol) ===
            md = app_state.get("market_data", {}).get("buffer")
            if md is not None:
                md.update_tick(symbol, price_raw, volume=volume, timestamp=now)

                # old log line used global recent_prices; now log this symbol’s tail
                tail = md.get_recent_prices(symbol, limit=5)
                logging.debug(f"[MarketDataBuffer] {symbol} recent prices (tail): {tail}")
            else:
                logging.warning("[MarketDataBuffer] Missing app_state['market_data']['buffer']; skipping update.")

            has_position, can_sell = check_local_position(symbol, app_state["open_trades"])
            if not has_position:
                try:
                    has_position, can_sell = check_position_status(symbol)
                except Exception as e:
                    logging.warning(f"[{timestamp}] ⚠️ Position fallback failed: {e}")

            # Step 1: Track max price since buy
            if has_position:
                trade_info = app_state["open_trades"].get(symbol)
                if trade_info:
                    current_max = trade_info.get("max_price", trade_info["buy_price"])
                    if price_raw > current_max:
                        trade_info["max_price"] = price_raw
                        logging.debug(f"[PeakTrack] Updated max price for {symbol}: {price_raw:.2f}")

            
            # === Run strategies ===
            # ✅ Unpack (signal, strategy_results)
            signal, strategy_results = strategy_manager.evaluate_trade(price_raw, has_position, can_sell, symbol)
            app_state["strategy"]["last_strategy_results"] = strategy_results
            logging.info(f"[HandleTrade] Strategy returned signal={signal}")

            # ✅ Execution-side decision snapshot
            allow_multiple = app_state.get("stream", {}).get("allow_multiple_positions", False)
            in_trade_cooldown = app_state.get("utils", {}).get("trades_utils", {}).get("in_trade_cooldown", False)
            open_trade_info = app_state.get("open_trades", {}).get(symbol)

            logging.info(
                "[ExecutionCheck] %s | signal=%s has_position=%s can_sell=%s "
                "allow_multiple_positions=%s in_trade_cooldown=%s open_trade=%s",
                symbol,
                signal,
                has_position,
                can_sell,
                allow_multiple,
                in_trade_cooldown,
                open_trade_info,
            )
            
            logging.info(
                "[TradeGate] %s | signal=%s has_position=%s can_sell=%s cooldown=%s allow_multi=%s open_trade=%s",
                symbol,
                signal,
                has_position,
                can_sell,
                app_state["utils"]["trades_utils"].get("in_trade_cooldown"),
                app_state["stream"].get("allow_multiple_positions", False),
                app_state.get("open_trades", {}).get(symbol),
            )

            price = float(f"{price_raw:.2f}")
            logging.info(f"[{timestamp}] 📥 Trade: {symbol} | Price: ${price:.2f} | Size: {volume}")

            app_state["last_trade_price_by_symbol"][symbol] = price
            app_state["strategy"]["signal_history"].append(signal)
            app_state["utils"]["trades_utils"]["trade_timestamps"].append(time.time())

            self._last_trade_handled = {
                "symbol": symbol,
                "price": price,
                "timestamp": timestamp
            }
            self._update_debug_snapshot("last_trade", self._last_trade_handled)

            if app_state["utils"]["trades_utils"]["in_trade_cooldown"]:
                logging.warning("⏳ Trade blocked — cooldown active.")
                logging.warning(
                    "[ExecutionBlock] %s blocked by in_trade_cooldown=True",
                    symbol
                )
                return

            if signal == "buy":
                logging.info(f"[HandleTrade] 🟢 BUY signal detected — entering buy logic")
                # 💡 Smart safeguard: avoid duplicate buys
                if ( has_position or not can_sell ) and not app_state["stream"].get("allow_multiple_positions", False):
                    logging.warning(f"[{timestamp}] 🛑 Buy skipped — already holding {symbol} position.")
                    # cancel_open_limit_orders(symbol)  # Optional: cancel any pending limit buys
                    logging.warning(
                        "[ExecutionBlock] %s buy blocked | has_position=%s can_sell=%s allow_multiple_positions=%s",
                        symbol,
                        has_position,
                        can_sell,
                        app_state["stream"].get("allow_multiple_positions", False),
                    )
                    send_email_alert("Buy Blocked", f"Buy signal confirmed for {symbol}, but bot is already holding position.\n\nPrice: ${price:.2f} @ {timestamp}")
                    return

                try:
                    await self._execute_buy(symbol, price)
                except Exception as e:
                    logging.exception(f"❌ Unexpected error during BUY execution for {symbol}: {e}")
                    send_email_alert("❌ BUY Execution Crash", str(e))
                    
            elif signal == "sell":
                logging.info(
                    "[ExecutionCheck] %s entering sell path | has_position=%s can_sell=%s open_trade=%s",
                    symbol,
                    has_position,
                    can_sell,
                    app_state.get("open_trades", {}).get(symbol),
                )
                try:
                    await self._execute_sell(symbol, price)
                except Exception as e:
                    logging.exception(f"❌ Unexpected error during SELL execution for {symbol}: {e}")
                    send_email_alert("❌ SELL Execution Crash", str(e))
            else:
                logging.debug(f"💤 No action for {symbol} — signal=None, has_position={has_position}.")

        except Exception as e:
            logging.exception(f"Unhandled exception in handle_trade: {e}")
            send_email_alert("❌ Trade Handling Crash", str(e))
        finally:
            active_handle_trade.reset(token)
            app_state["main"]["async_tasks"].discard(task)

    
    async def _execute_buy(self, symbol, price):
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        try:
            if not symbol or price is None or price <= 0:
                raise ValueError(f"Invalid symbol or price: {symbol}, {price}")

            app_state.setdefault("last_buy_order_time_by_symbol", {})
            last_buy_time = app_state["last_buy_order_time_by_symbol"].get(symbol, 0)

            if time.time() - last_buy_time < 30:
                logging.info(f"[{timestamp}] ⏳ Throttling BUY order placement for {symbol}.")
                return

            # === Clock status ===
            try:
                clock = app_state["trading_client"].get_clock()
                market_is_open = clock.is_open
            except Exception as e:
                logging.error(f"[{timestamp}] ⚠️ Could not determine market status: {e}")
                send_email_alert("⚠️ Market Status Check Failed", str(e))
                return

            # === Account info & budget ===
            try:
                account = app_state["trading_client"].get_account()
                buying_power = float(account.buying_power)

                equity = float(account.equity)
                allocation = 0.05
                budget = min(buying_power, equity * allocation)

                if budget < price:
                    logging.warning(f"[{timestamp}] ⚠️ Not enough buying power (${budget:.2f}) to buy 1 share at ${price:.2f}")
                    return

                # NOTE:
                # qty_to_buy is intentionally calculated but not yet used for live order sizing.
                # For now, the bot stays on fixed-size order behavior while strategy quality
                # is validated against baseline performance. Keep qty_to_buy here so the
                # sizing logic is ready for future rollout.

                qty_to_buy = int(budget // price)
                if qty_to_buy == 0:
                    logging.warning(f"[{timestamp}] ⚠️ Computed quantity to buy is 0. Budget=${budget:.2f}, Price=${price:.2f}")
                    return

            except Exception as e:
                logging.error(f"[{timestamp}] ❌ Failed to calculate buy quantity: {e}")
                send_email_alert("❌ Buy Quantity Calculation Failed", str(e))
                return

            # === Reconcile any existing order first ===
            decision, existing_order_id = reconcile_existing_order(
                symbol=symbol,
                side=OrderSide.BUY,
                new_price=price,
                new_qty=qty_to_buy,
                market_is_open=market_is_open,
            )

            if decision == "blocked_has_position":
                logging.warning(f"[{timestamp}] ⏹ BUY blocked for {symbol} — already holding or pending.")
                return

            if decision == "keep_existing":
                logging.info(f"[{timestamp}] ⏸ Existing BUY order kept for {symbol}: {existing_order_id}")
                return

            if decision == "replace_required":
                try:
                    app_state["trading_client"].cancel_order_by_id(existing_order_id)

                    for _ in range(10):
                        try:
                            old_order = app_state["trading_client"].get_order_by_id(existing_order_id)
                            old_status = str(getattr(old_order, "status", "")).lower()
                            if old_status in {"canceled", "expired", "rejected", "filled", "done_for_day"}:
                                break
                        except Exception:
                            break
                        await asyncio.sleep(0.2)

                    logging.info(
                        f"[{timestamp}] 🔁 Cancelled old BUY order {existing_order_id} before replacement"
                    )

                    clear_tracked_order(symbol)
                except Exception as e:
                    logging.warning(
                        f"[{timestamp}] Failed to cancel existing BUY order {existing_order_id}: {e}"
                    )
                    return

            # === Order Construction ===
            order = create_order_request(
                symbol,
                1,
                side=OrderSide.BUY,
                price=price,
                market_is_open=market_is_open
            )

            submitted_order = app_state["trading_client"].submit_order(order)
            order_id = getattr(submitted_order, "id", None)

            logging.info(f"[{timestamp}] ✅ Buy order for {symbol} submitted: {submitted_order}")

            if not order_id:
                logging.error(f"[{timestamp}] ❌ No order id returned for BUY order on {symbol}.")
                send_email_alert(f"❌ Buy Order Failed for {symbol}", "No order id returned from submitted order.")
                return

            # Throttle future order submissions immediately after successful submit
            app_state["last_buy_order_time_by_symbol"][symbol] = time.time()

            # 🧠 Immediately mark as pending
            app_state.setdefault("open_trades", {})[symbol] = {
                "order_id": order_id,
                "buy_price": price,
                "buy_time": datetime.now(timezone.utc),
                "quantity": qty_to_buy,
                "status": "pending"
            }

            await asyncio.sleep(1)
            status = str(app_state["trading_client"].get_order_by_id(order_id).status).lower()

            if status == "filled":
                app_state["open_trades"][symbol]["status"] = "filled"
                app_state["last_trade_price_by_symbol"][symbol] = price
                app_state["last_trade_time"] = time.time()
                app_state["last_signal"] = "buy"
                send_email_alert("✅ Buy Executed", f"✅ Buy Executed for {symbol} at ${price:.2f} @ {timestamp} UTC")
            else:
                logging.warning(f"[{timestamp}] ⚠️ Buy order for {symbol} not filled. Status: {status}")
                track_limit_order(
                    symbol,
                    order_id,
                    side=OrderSide.BUY,
                    qty=qty_to_buy,
                    limit_price=price,
                    market_is_open=market_is_open,
                )

        except Exception as e:
            logging.error(f"[{timestamp}] ❌ Error submitting BUY order for {symbol}: {e}")
            send_email_alert(f"❌ Buy Order Failed for {symbol}", str(e))

    
    async def _execute_sell(self, symbol, price):
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        sells_in_progress = self.app_state["strategy"].setdefault("sells_in_progress", set())

        try:
            if not symbol or price is None or price <= 0:
                raise ValueError(f"Invalid symbol or price: {symbol}, {price}")

            # === Market status ===
            try:
                clock = app_state["trading_client"].get_clock()
                market_is_open = clock.is_open
            except Exception as e:
                logging.error(f"[{timestamp}] ⚠️ Could not determine market status: {e}")
                send_email_alert("⚠️ Market Status Check Failed", str(e))
                return

            # NOTE:
            # qty_to_sell is intentionally calculated but not yet used for live liquidation sizing.
            # Current behavior keeps sell execution on the existing fixed-size path until
            # position sizing is promoted to a production decision.

            # === Quantity to sell ===
            qty_to_sell = 0
            try:
                positions = app_state["trading_client"].get_all_positions()
                for pos in positions:
                    if pos.symbol == symbol:
                        qty_to_sell = int(float(pos.qty))
                        break

                if qty_to_sell <= 0:
                    logging.warning(f"[{timestamp}] ⚠️ No shares found to sell for {symbol} — rechecking Alpaca...")
                    positions = app_state["trading_client"].get_all_positions()
                    for pos in positions:
                        if pos.symbol == symbol:
                            qty_to_sell = int(float(pos.qty))
                            break
            except Exception as e:
                logging.error(f"[{timestamp}] ❌ Failed to fetch positions for {symbol}: {e}")
                send_email_alert(f"❌ Sell Position Check Failed for {symbol}", str(e))
                return

            if qty_to_sell <= 0:
                logging.warning(f"[{timestamp}] ❌ Confirmed — no {symbol} shares held. Removing stale open_trades entry.")
                app_state["open_trades"].pop(symbol, None)
                return

            # === Self-heal stale sell guard ===
            if symbol in sells_in_progress:
                try:
                    params = GetOrdersRequest(status=QueryOrderStatus.OPEN)
                    open_orders = list(app_state["trading_client"].get_orders(filter=params))

                    has_open_sell = any(
                        getattr(o, "symbol", None) == symbol and normalize_side(getattr(o, "side", "")) == "sell"
                        for o in open_orders
                    )

                    if not has_open_sell:
                        logging.warning(f"[SellGuard] Clearing stale in-progress flag for {symbol} (no open sell found).")
                        sells_in_progress.discard(symbol)

                except Exception as e:
                    logging.warning(f"[SellGuard] Could not check open orders: {e}")

            if symbol in sells_in_progress:
                logging.warning(f"⏹ Duplicate sell blocked — sell already in progress for {symbol}.")
                return

            # === Submit sell order ===
            decision, existing_order_id = reconcile_existing_order(
                symbol=symbol,
                side=OrderSide.SELL,
                new_price=price,
                new_qty=qty_to_sell,
                market_is_open=market_is_open,
            )

            if decision == "blocked_no_position":
                logging.warning(f"[{timestamp}] ⏹ SELL blocked for {symbol} — no position exists.")
                app_state["open_trades"].pop(symbol, None)
                sells_in_progress.discard(symbol)
                return

            if decision == "keep_existing":
                logging.info(f"[{timestamp}] ⏸ Existing SELL order kept for {symbol}: {existing_order_id}")
                sells_in_progress.add(symbol)
                app_state.setdefault("sell_guard_times", {})[symbol] = time.time()
                return

            if decision == "replace_required":
                try:
                    app_state["trading_client"].cancel_order_by_id(existing_order_id)
                    
                    for _ in range(10):
                        try:
                            old_order = app_state["trading_client"].get_order_by_id(existing_order_id)
                            old_status = str(getattr(old_order, "status", "")).lower()
                            if old_status in {"canceled", "expired", "rejected", "filled", "done_for_day"}:
                                break
                        except Exception:
                            break
                        await asyncio.sleep(0.2)
                                        
                    logging.info(
                        f"[{timestamp}] 🔁 Cancelled old SELL order {existing_order_id} before replacement"
                    )

                    clear_tracked_order(symbol)
                except Exception as e:
                    logging.warning(
                        f"[{timestamp}] Failed to cancel existing SELL order {existing_order_id}: {e}"
                    )
                    sells_in_progress.discard(symbol)
                    return
    
            order = create_order_request(
                symbol,
                1,
                side=OrderSide.SELL,
                price=price,
                market_is_open=market_is_open
            )
            submitted_order = app_state["trading_client"].submit_order(order)
            order_id = getattr(submitted_order, "id", None)

            logging.info(f"[{timestamp}] ✅ Sell order for {symbol} submitted: {submitted_order}")

            if not order_id:
                logging.error(f"[{timestamp}] ❌ No order id returned; not setting in-progress guard.")
                return

            sells_in_progress.add(symbol)
            app_state.setdefault("sell_guard_times", {})[symbol] = time.time()

            if symbol in app_state["open_trades"]:
                app_state["open_trades"][symbol]["status"] = "pending_sell"

            # === Check immediate fill ===
            await asyncio.sleep(1)
            status = str(app_state["trading_client"].get_order_by_id(order_id).status).lower()

            if status == "filled":
                trade_info = app_state.setdefault("open_trades", {}).pop(symbol, None)
                sell_time = datetime.now(timezone.utc)

                if trade_info:
                    buy_price = trade_info.get("buy_price", price)
                    profit_loss = price - buy_price

                    trade_type = app_state["strategy"].get("last_exit_reason", "Standard")
                    log_trade_to_summary(symbol, buy_price, trade_info["buy_time"], price, sell_time, trade_type)
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
                    logging.warning(f"[{timestamp}] ⚠️ No matching open trade record for {symbol}")

                app_state["last_signal"] = "sell"
                send_email_alert("✅ Sell Executed", f"✅ Sell Executed for {symbol} at ${price:.2f} @ {timestamp} UTC")
                sells_in_progress.discard(symbol)
                return

            # === Not filled immediately ===
            logging.warning(f"[{timestamp}] ⚠️ Sell order for {symbol} not filled. Status: {status}")
            track_limit_order(
                symbol,
                order_id,
                side=OrderSide.SELL,
                qty=qty_to_sell,
                limit_price=price,
                market_is_open=market_is_open,
            )
            logging.info(f"[{timestamp}] 🕵️ Sell order for {symbol} not filled immediately — tracking for completion...")

            return

        except Exception as e:
            logging.error(f"[{timestamp}] ❌ Outer-level error in _execute_sell: {e}")
            send_email_alert("❌ Outer Error in SELL Flow", str(e))
            sells_in_progress.discard(symbol)

    def _thread_target(self):
        self._loop = asyncio.new_event_loop()
        app_state["stream"]["loop"] = self._loop
        asyncio.set_event_loop(self._loop)

        try:
            self._manage_task = self._loop.create_task(self._manage_stream())
            self._loop.run_until_complete(self._manage_task)

        except asyncio.CancelledError:
            logging.info("Stream manage task cancelled during shutdown.")

        except RuntimeError as e:
            logging.warning(f"Stream loop runtime exit: {e}")

        finally:
            try:
                if self._loop and not self._loop.is_closed():
                    pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]

                    for t in pending:
                        t.cancel()

                    if pending:
                        self._loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )

            except Exception:
                logging.exception("⚠️ Error draining pending tasks during stream thread shutdown.")

            finally:
                try:
                    if self._loop and not self._loop.is_closed():
                        self._loop.close()
                except Exception:
                    logging.exception("⚠️ Error closing stream loop.")
                finally:
                    self._loop = None
                    app_state["stream"]["loop"] = None
                    self._thread = None
                    app_state["stream"]["thread"] = None
                    self._running = False
                    app_state["stream"]["running"] = False
                    app_state["stream"]["stopping"] = False
                    app_state["stream"]["state"] = "stopped"
                    self._manage_task = None
                    self._stream = None
                    app_state["stream"]["instance"] = None
                    logging.info("Stream thread fully stopped")

    def start(self):
        with self._connection_lock:
            current_state = app_state["stream"].get("state", "stopped")
            if current_state in {"starting", "running", "stopping"}:
                logging.warning(f"Stream start blocked: current state is {current_state}.")
                return

            app_state["stream"]["state"] = "starting"
            app_state["stream"]["stopping"] = False
            self._running = False
            app_state["stream"]["running"] = False

        self._shutdown_event.clear()
        self._thread = safe_thread(self._thread_target, name="TradeStreamThread", daemon=True)
        app_state["stream"]["thread"] = self._thread
        logging.info("🚀 Trade stream started in background thread.")
        
    def stop(self):
        with self._connection_lock:
            current_state = app_state["stream"].get("state", "stopped")

            if current_state == "stopped":
                logging.info("Stream already stopped.")
                app_state["stream"]["stopping"] = False
                return

            if current_state == "stopping":
                logging.info("Stream stop already in progress.")
                return

            app_state["stream"]["state"] = "stopping"
            app_state["stream"]["stopping"] = True
            self._running = False
            app_state["stream"]["running"] = False

        start_time = time.time()
        self._shutdown_event.set()

        loop = self._loop
        thread = self._thread

        if loop and loop.is_running():

            async def _shutdown_async():
                try:
                    if self._manage_task and not self._manage_task.done():
                        self._manage_task.cancel()
                        await asyncio.gather(self._manage_task, return_exceptions=True)
                        logging.info("✅ Manage task cancelled cleanly.")
                except Exception:
                    logging.exception("⚠️ Failed to cancel/manage stream task during shutdown.")

                try:
                    if self._stream:
                        try:
                            await asyncio.wait_for(self._stream.stop(), timeout=5)
                            logging.info("✅ Alpaca stream shut down cleanly.")
                        except asyncio.TimeoutError:
                            logging.warning("⚠️ Timed out stopping Alpaca stream after task cancellation.")
                        except Exception:
                            logging.exception("⚠️ Error during Alpaca stop()")
                        finally:
                            self._stream = None
                            app_state["stream"]["instance"] = None
                except Exception:
                    logging.exception("⚠️ Unexpected error in _shutdown_async cleanup.")

            fut = asyncio.run_coroutine_threadsafe(_shutdown_async(), loop)

            try:
                fut.result(timeout=8)
            except concurrent.futures.CancelledError:
                logging.info("Shutdown coroutine was cancelled during loop teardown.")
            except (concurrent.futures.TimeoutError, TimeoutError):
                logging.warning("⚠️ Shutdown coroutine did not finish in time; continuing with thread join.")
            except Exception:
                logging.exception("⚠️ Error while waiting for shutdown coroutine.")

            # IMPORTANT:
            # Do NOT call loop.stop() here.
            # Let the stream thread drain tasks and close the loop itself.

        if thread:
            thread.join(timeout=8)
            if thread.is_alive():
                logging.warning("⚠️ TradeStreamThread still alive after join timeout.")
            else:
                self._thread = None
                app_state["stream"]["thread"] = None

        self._loop = None
        app_state["stream"]["loop"] = None
        self._manage_task = None

        if not thread or not thread.is_alive():
            self._thread = None
            app_state["stream"]["thread"] = None
            self._stream = None
            app_state["stream"]["instance"] = None
            app_state["stream"]["state"] = "stopped"
            app_state["stream"]["stopping"] = False

        elapsed = time.time() - start_time
        logging.info(f"🛑 Trade stream stopped (stop() complete) in {elapsed:.2f}s.")
        app_state["stream"]["last_shutdown_duration"] = elapsed