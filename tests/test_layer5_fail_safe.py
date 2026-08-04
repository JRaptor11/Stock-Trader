from dataclasses import dataclass
from enum import Enum
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class QueryOrderStatus(Enum):
    OPEN = "open"


class TimeInForce(Enum):
    DAY = "day"


class GetOrdersRequest:
    def __init__(self, status=None):
        self.status = status


class MarketOrderRequest:
    def __init__(self, symbol, qty, side, time_in_force):
        self.symbol = symbol
        self.qty = qty
        self.side = side
        self.time_in_force = time_in_force


class LimitOrderRequest(MarketOrderRequest):
    def __init__(self, symbol, qty, side, time_in_force, limit_price, extended_hours):
        super().__init__(symbol, qty, side, time_in_force)
        self.limit_price = limit_price
        self.extended_hours = extended_hours


alpaca = types.ModuleType("alpaca")
alpaca_trading = types.ModuleType("alpaca.trading")
alpaca_enums = types.ModuleType("alpaca.trading.enums")
alpaca_requests = types.ModuleType("alpaca.trading.requests")
alpaca_enums.OrderSide = OrderSide
alpaca_enums.QueryOrderStatus = QueryOrderStatus
alpaca_enums.TimeInForce = TimeInForce
alpaca_requests.GetOrdersRequest = GetOrdersRequest
alpaca_requests.MarketOrderRequest = MarketOrderRequest
alpaca_requests.LimitOrderRequest = LimitOrderRequest
sys.modules.setdefault("alpaca", alpaca)
sys.modules.setdefault("alpaca.trading", alpaca_trading)
sys.modules.setdefault("alpaca.trading.enums", alpaca_enums)
sys.modules.setdefault("alpaca.trading.requests", alpaca_requests)
alerts = types.ModuleType("integrations.alerts")
alerts.send_email_alert = lambda *args, **kwargs: None
sys.modules.setdefault("integrations.alerts", alerts)
trade_utils = types.ModuleType("trading.trade_utils")
trade_utils.log_trade_to_csv = lambda *args, **kwargs: None
trade_utils.log_trade_to_summary = lambda *args, **kwargs: None
sys.modules.setdefault("trading.trade_utils", trade_utils)

from core.state import app_state, fail_safe_event
from layers import layer5_executor
from layers.layer3_rebalancer import _global_fail_safe_active
from safety import fail_safe_lifecycle as lifecycle
from trading import orders as order_tracking


@dataclass
class Position:
    symbol: str
    qty: float
    market_value: float = 0.0
    avg_entry_price: float = 100.0


@dataclass
class BrokerOrder:
    symbol: str
    side: object
    status: str = "accepted"
    id: str = "existing"
    filled_qty: float = 0.0
    limit_price: float | None = None
    created_at: object = None
    submitted_at: object = None


class Client:
    def __init__(self, positions, open_orders=None):
        self.positions = list(positions)
        self.open_orders = list(open_orders or [])
        self.submissions = []
        self._lock = threading.Lock()

    def get_clock(self):
        return types.SimpleNamespace(is_open=True)

    def get_orders(self, filter=None):
        with self._lock:
            return list(self.open_orders)

    def get_all_positions(self):
        return list(self.positions)

    def get_account(self):
        return types.SimpleNamespace(
            cash=100000,
            equity=100000,
            buying_power=100000,
        )

    def submit_order(self, request):
        time.sleep(0.01)
        with self._lock:
            order = BrokerOrder(
                symbol=request.symbol,
                side=request.side,
                id=f"order-{len(self.submissions) + 1}",
            )
            self.submissions.append(request)
            self.open_orders.append(order)
            return order


class RejectingClient(Client):
    def submit_order(self, request):
        raise RuntimeError("broker rejected test order")


class ClosedMarketClient(Client):
    def get_clock(self):
        return types.SimpleNamespace(is_open=False)


class SellSettlingClient(Client):
    def __init__(self):
        super().__init__([Position("AMD", 10, avg_entry_price=100)])
        self.cash = 0.0
        self.orders_by_id = {}

    def get_account(self):
        return types.SimpleNamespace(
            cash=self.cash,
            equity=100000,
            buying_power=100000,
        )

    def submit_order(self, request):
        order = BrokerOrder(
            symbol=request.symbol,
            side=request.side,
            status="filled",
            id=f"order-{len(self.submissions) + 1}",
            filled_qty=request.qty,
        )
        order.filled_avg_price = 100.0
        self.submissions.append(request)
        self.orders_by_id[order.id] = order
        if request.side == OrderSide.SELL:
            self.positions = []
            self.cash += request.qty * 100.0
        return order

    def get_order_by_id(self, order_id):
        return self.orders_by_id[order_id]


class Layer5FailSafeTests(unittest.TestCase):
    def setUp(self):
        fail_safe_event.clear()
        app_state["fail_safes"] = {
            "state": False,
            "symbols": set(),
            "pending_liquidation_symbols": [],
            "lifecycles": {},
            "global_active": False,
            "liquidate_all": False,
            "symbol": None,
            "last_trigger_reason": None,
        }
        app_state["execution"] = {
            "layer4_execution_enabled": True,
            "layer4_market_hours_only": True,
            "layer5_broker_error_cooldowns": {},
        }
        app_state["layers"] = {}
        app_state["open_orders"] = {}
        app_state["open_trades"] = {
            "AMD": {"status": "filled", "buy_price": 100.0, "quantity": 10}
        }
        app_state["last_trade_price_by_symbol"] = {"AMD": 90.0, "NVDA": 120.0}
        app_state["strategy"].setdefault("sells_in_progress", set()).clear()

        self.patches = [
            patch.object(layer5_executor, "track_limit_order", lambda **kwargs: None),
            patch.object(layer5_executor, "register_layer5_cycle_submissions", lambda *a, **k: None),
            patch.object(layer5_executor, "append_layer4_order_rows", lambda *a, **k: None),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()

    def execute(self, client, plan=None, summary=None):
        app_state["trading_client"] = client
        return layer5_executor.execute_layer5_plan(
            plan or [],
            summary or {"cycle_id": "test", "plan_id": "test"},
        )

    def test_per_stock_allows_unrelated_buy_and_forces_full_sell(self):
        lifecycle.queue_liquidations(["AMD"], reason="per_stock", scope="per_stock")
        client = Client([Position("AMD", 10)])
        plan = [
            {
                "symbol": "NVDA",
                "decision": "BUY",
                "qty": 1,
                "price": 120,
                "notional": 120,
            }
        ]
        result = self.execute(client, plan)
        self.assertEqual(2, result["submitted"])
        self.assertEqual({"AMD", "NVDA"}, {request.symbol for request in client.submissions})
        amd = next(request for request in client.submissions if request.symbol == "AMD")
        self.assertEqual(10, amd.qty)
        amd_result = next(
            order for order in result["orders"]
            if order.get("symbol") == "AMD"
        )
        self.assertEqual(
            "risk_or_fail_safe",
            amd_result["trade_attribution"],
        )
        self.assertFalse(_global_fail_safe_active())

    def test_global_blocks_all_buys(self):
        lifecycle.queue_liquidations(["AMD"], reason="global", scope="global")
        client = Client([Position("AMD", 10)])
        plan = [{"symbol": "NVDA", "decision": "BUY", "qty": 1, "price": 120}]
        result = self.execute(client, plan)
        self.assertEqual(1, result["submitted"])
        self.assertEqual(["AMD"], [request.symbol for request in client.submissions])
        self.assertTrue(
            any(order.get("reason") == "fail_safe_active_blocks_buy" for order in result["orders"])
        )
        self.assertTrue(_global_fail_safe_active())

    def test_existing_sell_order_prevents_duplicate(self):
        lifecycle.queue_liquidations(["AMD"], reason="per_stock", scope="per_stock")
        existing = BrokerOrder("AMD", OrderSide.SELL, id="existing")
        client = Client([Position("AMD", 10)], [existing])
        lifecycle.reconcile(client=client)
        result = self.execute(client)
        self.assertEqual(0, result["submitted"])
        self.assertEqual([], client.submissions)

    def test_unrelated_open_order_does_not_delay_fail_safe(self):
        lifecycle.queue_liquidations(["AMD"], reason="per_stock", scope="per_stock")
        unrelated = BrokerOrder("NVDA", OrderSide.BUY, id="unrelated")
        client = Client([Position("AMD", 10)], [unrelated])
        result = self.execute(client)
        self.assertEqual(1, result["submitted"])
        self.assertEqual("AMD", client.submissions[0].symbol)

    def test_retry_uses_remaining_broker_quantity(self):
        lifecycle.queue_liquidations(["AMD"], reason="per_stock", scope="per_stock")
        state = app_state["fail_safes"]["lifecycles"]["AMD"]
        state["lifecycle_state"] = "waiting_retry"
        state["next_retry_at_epoch"] = 0
        client = Client([Position("AMD", 6)])
        self.execute(client)
        self.assertEqual(6, client.submissions[0].qty)

    def test_broker_rejection_keeps_lifecycle_for_cooldown_retry(self):
        lifecycle.queue_liquidations(["AMD"], reason="per_stock", scope="per_stock")
        client = RejectingClient([Position("AMD", 10)])
        result = self.execute(client)
        state = app_state["fail_safes"]["lifecycles"]["AMD"]
        self.assertEqual(1, result["errors"])
        self.assertEqual("waiting_retry", state["lifecycle_state"])
        self.assertIn("broker rejected", state["last_error"])
        self.assertFalse(lifecycle.eligible_for_submission("AMD"))

    def test_market_closed_retains_queued_liquidation(self):
        lifecycle.queue_liquidations(["AMD"], reason="per_stock", scope="per_stock")
        client = ClosedMarketClient([Position("AMD", 10)])
        with patch.object(
            layer5_executor,
            "_fail_safe_after_hours_window",
            return_value=(False, "outside_test_window"),
        ):
            result = self.execute(client)
        self.assertEqual("market_closed", result["blocked_reason"])
        self.assertEqual(
            "queued",
            app_state["fail_safes"]["lifecycles"]["AMD"]["lifecycle_state"],
        )
        self.assertTrue(lifecycle.snapshot()["active"])

    def test_after_hours_fail_safe_submits_extended_limit_sell(self):
        lifecycle.queue_liquidations(
            ["AMD"], reason="per_stock", scope="per_stock"
        )
        client = ClosedMarketClient([Position("AMD", 10)])
        quote = {
            "ok": True,
            "reason": "fresh_quote",
            "bid": 89.5,
            "ask": 90.0,
            "spread_pct": 0.56,
            "quote_age_seconds": 1.0,
            "limit_price": 89.46,
        }
        with (
            patch.object(
                layer5_executor,
                "_fail_safe_after_hours_window",
                return_value=(True, "after_hours_4pm_8pm_et"),
            ),
            patch.object(
                layer5_executor,
                "_fail_safe_extended_quote",
                return_value=quote,
            ),
        ):
            result = self.execute(client)
        self.assertEqual(1, result["submitted"])
        request = client.submissions[0]
        self.assertEqual(OrderSide.SELL, request.side)
        self.assertEqual(89.46, request.limit_price)
        self.assertTrue(request.extended_hours)
        self.assertTrue(
            result["orders"][0]["extended_hours_fail_safe"]
        )

    def test_post_liquidation_cooldown_blocks_same_symbol_buy(self):
        app_state["fail_safes"]["reentry_block_until"] = {
            "AMD": time.time() + 3600
        }
        client = Client([])
        result = self.execute(
            client,
            plan=[{
                "symbol": "AMD",
                "decision": "BUY",
                "qty": 2,
                "price": 100,
                "notional": 200,
            }],
        )
        self.assertEqual(0, result["submitted"])
        self.assertEqual(
            "fail_safe_reentry_cooldown_no_rows",
            result["blocked_reason"],
        )
        self.assertTrue(any(
            row.get("reason") == "fail_safe_reentry_cooldown_blocks_buy"
            for row in result["orders"]
        ))

    def test_fail_safe_bypasses_strategy_restart_block(self):
        lifecycle.queue_liquidations(["AMD"], reason="per_stock", scope="per_stock")
        client = Client([Position("AMD", 10)])
        result = self.execute(
            client,
            plan=[{"symbol": "NVDA", "decision": "BUY", "qty": 1, "price": 120}],
            summary={
                "cycle_id": "test",
                "plan_id": "test",
                "strategy_execution_blocked_reason": "restart_recovery",
            },
        )
        self.assertEqual(1, result["submitted"])
        self.assertEqual("AMD", client.submissions[0].symbol)

    def test_two_concurrent_execution_passes_submit_once(self):
        lifecycle.queue_liquidations(["AMD"], reason="per_stock", scope="per_stock")
        client = Client([Position("AMD", 10)])
        app_state["trading_client"] = client
        results = []

        def run():
            results.append(
                layer5_executor.execute_layer5_plan(
                    [],
                    {"cycle_id": "race", "plan_id": "race"},
                )
            )

        threads = [threading.Thread(target=run), threading.Thread(target=run)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, len(client.submissions))

    def test_terminal_buy_syncs_aggregate_broker_position(self):
        client = Client([Position("AMD", 15, avg_entry_price=105)])
        app_state["trading_client"] = client
        order = BrokerOrder(
            "AMD",
            OrderSide.BUY,
            status="filled",
            id="buy-1",
            filled_qty=5,
        )
        order.filled_avg_price = 110

        order_tracking._finalize_filled_buy("AMD", order, "buy-1")

        self.assertEqual(15, app_state["open_trades"]["AMD"]["quantity"])
        self.assertEqual(105, app_state["open_trades"]["AMD"]["buy_price"])
        self.assertEqual("synced", app_state["open_trades"]["AMD"]["status"])

    def test_terminal_partial_sell_preserves_remaining_broker_position(self):
        client = Client([Position("AMD", 6, avg_entry_price=100)])
        app_state["trading_client"] = client
        order = BrokerOrder(
            "AMD",
            OrderSide.SELL,
            status="filled",
            id="sell-1",
            filled_qty=4,
        )
        order.filled_avg_price = 99

        order_tracking._finalize_filled_sell("AMD", order, "sell-1")

        self.assertEqual(6, app_state["open_trades"]["AMD"]["quantity"])
        self.assertEqual("synced", app_state["open_trades"]["AMD"]["status"])

    def test_sell_phase_refreshes_cash_before_buy_phase(self):
        client = SellSettlingClient()
        plan = [
            {
                "symbol": "NVDA",
                "decision": "BUY",
                "qty": 5,
                "price": 100,
                "notional": 500,
            },
            {
                "symbol": "AMD",
                "decision": "SELL",
                "qty": 10,
                "price": 100,
                "notional": 1000,
            },
        ]

        result = self.execute(client, plan)

        self.assertEqual(["AMD", "NVDA"], [
            request.symbol for request in client.submissions
        ])
        self.assertEqual(2, result["submitted"])
        self.assertEqual(1000, result["cash_after_sell_refresh"])
        self.assertEqual(1, result["sell_phase_wait"]["terminal_order_count"])


if __name__ == "__main__":
    unittest.main()
