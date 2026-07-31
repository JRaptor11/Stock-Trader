import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import sys
import threading
import types
import unittest
from unittest.mock import patch


# Keep imports offline and independent of optional alert/CSV dependencies.
alerts = types.ModuleType("integrations.alerts")
alerts.send_email_alert = lambda *args, **kwargs: None
sys.modules.setdefault("integrations.alerts", alerts)
trade_utils = types.ModuleType("trading.trade_utils")
trade_utils.log_trade_to_csv = lambda *args, **kwargs: None
trade_utils.log_trade_to_summary = lambda *args, **kwargs: None
sys.modules.setdefault("trading.trade_utils", trade_utils)

from core.state import app_state, fail_safe_event
from safety import fail_safe_lifecycle as lifecycle
from safety import fail_safes
from safety.fail_safes import fail_safe_liquidation_worker


@dataclass
class FakePosition:
    symbol: str
    qty: float
    avg_entry_price: float = 100.0


@dataclass
class FakeOrder:
    symbol: str
    side: str = "sell"
    status: str = "accepted"
    id: str = "order-1"
    filled_qty: float = 0.0


class FakeClient:
    def __init__(self, positions=None, orders=None):
        self.positions = list(positions or [])
        self.orders = list(orders or [])

    def get_all_positions(self):
        return list(self.positions)

    def get_orders(self, filter=None):
        return list(self.orders)


class FailSafeLifecycleTests(unittest.TestCase):
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
        app_state["stream"]["shutdown_event"] = threading.Event()
        app_state["trading_client"] = None
        app_state["open_trades"] = {}
        app_state["last_trade_price_by_symbol"] = {}
        app_state["execution"] = {"layer4_execution_enabled": True}

    def queue_amd(self):
        return lifecycle.queue_liquidations(
            ["AMD"],
            reason="per_stock",
            scope="per_stock",
            trigger_price=90.0,
            entry_price=100.0,
            observed_loss_percent=10.0,
        )

    def test_repeated_trigger_creates_one_lifecycle_and_preserves_trigger(self):
        self.assertEqual(["AMD"], self.queue_amd())
        original = dict(app_state["fail_safes"]["lifecycles"]["AMD"])
        self.assertEqual([], self.queue_amd())
        current = app_state["fail_safes"]["lifecycles"]["AMD"]
        self.assertEqual(original["triggered_at"], current["triggered_at"])
        self.assertEqual(["AMD"], app_state["fail_safes"]["pending_liquidation_symbols"])

    def test_new_lifecycle_gets_new_generation_id_after_clear(self):
        self.queue_amd()
        first_id = app_state["fail_safes"]["lifecycles"]["AMD"]["lifecycle_id"]
        lifecycle.reconcile(positions=[], open_orders=[])
        self.queue_amd()
        second_id = app_state["fail_safes"]["lifecycles"]["AMD"]["lifecycle_id"]
        self.assertNotEqual(first_id, second_id)

    def test_submission_is_in_flight_not_cleared(self):
        self.queue_amd()
        self.assertTrue(lifecycle.mark_submission_started("AMD"))
        lifecycle.mark_submitted("AMD", FakeOrder(symbol="AMD"))
        state = app_state["fail_safes"]["lifecycles"]["AMD"]
        self.assertEqual("submitted_open", state["lifecycle_state"])
        self.assertEqual(["AMD"], app_state["fail_safes"]["pending_liquidation_symbols"])

    def test_partial_fill_remains_active(self):
        self.queue_amd()
        lifecycle.mark_submitted("AMD", FakeOrder(symbol="AMD"))
        lifecycle.record_order_update(
            "AMD",
            FakeOrder(symbol="AMD", status="partially_filled", filled_qty=4),
        )
        state = app_state["fail_safes"]["lifecycles"]["AMD"]
        self.assertEqual("partially_filled", state["lifecycle_state"])
        self.assertEqual(4.0, state["filled_qty"])
        self.assertTrue(lifecycle.snapshot()["active"])

    def test_complete_fill_with_remaining_position_waits_for_retry(self):
        self.queue_amd()
        lifecycle.mark_submitted("AMD", FakeOrder(symbol="AMD"))
        lifecycle.record_order_update(
            "AMD",
            FakeOrder(symbol="AMD", status="filled", filled_qty=10),
        )
        lifecycle.reconcile(
            positions=[FakePosition("AMD", 1)],
            open_orders=[],
        )
        state = app_state["fail_safes"]["lifecycles"]["AMD"]
        self.assertEqual("waiting_retry", state["lifecycle_state"])
        self.assertTrue(lifecycle.snapshot()["active"])

    def test_position_zero_reconciliation_clears_per_stock_state(self):
        self.queue_amd()
        lifecycle.mark_submitted("AMD", FakeOrder(symbol="AMD"))
        lifecycle.record_order_update(
            "AMD",
            FakeOrder(symbol="AMD", status="filled", filled_qty=10),
        )
        result = lifecycle.reconcile(positions=[], open_orders=[])
        self.assertEqual(["AMD"], result["cleared_symbols"])
        self.assertFalse(lifecycle.snapshot()["active"])
        self.assertFalse(fail_safe_event.is_set())

    def test_position_zero_starts_reentry_cooldown(self):
        self.queue_amd()
        lifecycle.mark_submission_started("AMD")
        lifecycle.mark_submitted("AMD", FakeOrder(symbol="AMD"))
        app_state.setdefault("config_defaults", {})[
            "FAIL_SAFE_REENTRY_COOLDOWN_SECONDS"
        ] = 3600
        with patch.object(lifecycle.time, "time", return_value=100.0):
            lifecycle.reconcile(positions=[], open_orders=[])
            self.assertTrue(lifecycle.should_block_buy("AMD"))
        with patch.object(lifecycle.time, "time", return_value=3700.0):
            self.assertFalse(lifecycle.should_block_buy("AMD"))

    def test_per_stock_blocks_only_same_symbol(self):
        self.queue_amd()
        self.assertTrue(lifecycle.should_block_buy("AMD"))
        self.assertFalse(lifecycle.should_block_buy("NVDA"))

    def test_global_blocks_all_buys_until_all_symbols_clear(self):
        lifecycle.queue_liquidations(
            ["AMD", "NVDA"],
            reason="global",
            scope="global",
        )
        self.assertTrue(lifecycle.should_block_buy("AAPL"))
        lifecycle.reconcile(
            positions=[FakePosition("NVDA", 2)],
            open_orders=[],
        )
        self.assertTrue(lifecycle.snapshot()["global_active"])
        lifecycle.reconcile(positions=[], open_orders=[])
        self.assertFalse(lifecycle.snapshot()["global_active"])
        self.assertFalse(lifecycle.should_block_buy("AAPL"))

    def test_global_trigger_escalates_existing_per_stock_lifecycle(self):
        self.queue_amd()
        lifecycle.queue_liquidations(["AMD"], reason="global", scope="global")
        self.assertEqual(
            "global",
            app_state["fail_safes"]["lifecycles"]["AMD"]["scope"],
        )
        lifecycle.reconcile(
            positions=[FakePosition("AMD", 1)],
            open_orders=[],
        )
        self.assertTrue(lifecycle.snapshot()["global_active"])

    def test_global_reconciliation_adopts_newly_held_symbol(self):
        lifecycle.queue_liquidations(["AMD"], reason="global", scope="global")
        lifecycle.reconcile(
            positions=[FakePosition("AMD", 1), FakePosition("NVDA", 2)],
            open_orders=[],
        )
        state = lifecycle.snapshot()
        self.assertEqual(["AMD", "NVDA"], state["symbols"])
        self.assertEqual("global", state["lifecycles"]["NVDA"]["scope"])

    def test_terminal_failure_waits_for_retry_cooldown(self):
        self.queue_amd()
        lifecycle.mark_submitted("AMD", FakeOrder(symbol="AMD"))
        with patch.object(lifecycle.time, "time", return_value=100.0):
            lifecycle.record_order_update(
                "AMD",
                FakeOrder(symbol="AMD", status="rejected"),
            )
        state = app_state["fail_safes"]["lifecycles"]["AMD"]
        self.assertEqual("waiting_retry", state["lifecycle_state"])
        self.assertFalse(lifecycle.eligible_for_submission("AMD", now_epoch=129.9))
        self.assertTrue(lifecycle.eligible_for_submission("AMD", now_epoch=130.0))

    def test_partially_filled_canceled_retries_remaining_position(self):
        self.queue_amd()
        lifecycle.mark_submitted("AMD", FakeOrder(symbol="AMD"))
        lifecycle.record_order_update(
            "AMD",
            FakeOrder(symbol="AMD", status="canceled", filled_qty=4),
        )
        lifecycle.reconcile(
            positions=[FakePosition("AMD", 6)],
            open_orders=[],
        )
        state = app_state["fail_safes"]["lifecycles"]["AMD"]
        self.assertEqual("waiting_retry", state["lifecycle_state"])
        self.assertEqual(6.0, state["remaining_broker_position_qty"])

    def test_existing_open_sell_restores_in_flight_and_prevents_retry(self):
        self.queue_amd()
        lifecycle.reconcile(
            positions=[FakePosition("AMD", 10)],
            open_orders=[FakeOrder(symbol="AMD", id="existing")],
        )
        state = app_state["fail_safes"]["lifecycles"]["AMD"]
        self.assertEqual("submitted_open", state["lifecycle_state"])
        self.assertEqual("existing", state["order_id"])
        self.assertFalse(lifecycle.eligible_for_submission("AMD"))

    def test_restart_remaining_position_without_order_becomes_retryable(self):
        self.queue_amd()
        lifecycle.mark_submission_started("AMD")
        lifecycle.mark_submitted("AMD", FakeOrder(symbol="AMD"))
        with patch.object(lifecycle.time, "time", return_value=200.0):
            lifecycle.reconcile(
                positions=[FakePosition("AMD", 3)],
                open_orders=[],
            )
        self.assertEqual(
            "waiting_retry",
            app_state["fail_safes"]["lifecycles"]["AMD"]["lifecycle_state"],
        )
        self.assertTrue(lifecycle.eligible_for_submission("AMD", now_epoch=230.0))

    def test_two_symbols_resolve_independently(self):
        lifecycle.queue_liquidations(
            ["AMD", "NVDA"],
            reason="per_stock",
            scope="per_stock",
        )
        lifecycle.reconcile(
            positions=[FakePosition("NVDA", 5)],
            open_orders=[],
        )
        state = lifecycle.snapshot()
        self.assertEqual(["NVDA"], state["symbols"])
        self.assertTrue(state["active"])

    def test_racing_queue_calls_create_one_lifecycle(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.queue_amd(), range(2)))
        self.assertEqual(1, sum(bool(result) for result in results))
        self.assertEqual(1, len(app_state["fail_safes"]["lifecycles"]))

    def test_submission_claim_is_atomic(self):
        self.queue_amd()
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: lifecycle.mark_submission_started("AMD"), range(2)))
        self.assertEqual(1, claims.count(True))

    def test_worker_executes_without_strategic_cycle(self):
        self.queue_amd()
        client = FakeClient(positions=[FakePosition("AMD", 10)], orders=[])
        app_state["trading_client"] = client
        calls = []
        fake_layer5 = types.ModuleType("layers.layer5_executor")

        def execute(plan, summary):
            calls.append((plan, summary))
            app_state["stream"]["shutdown_event"].set()
            return {"submitted": 1}

        fake_layer5.execute_layer5_plan = execute
        with patch.dict(sys.modules, {"layers.layer5_executor": fake_layer5}):
            asyncio.run(asyncio.wait_for(fail_safe_liquidation_worker(), timeout=1))

        self.assertEqual(1, len(calls))
        self.assertEqual([], calls[0][0])
        self.assertTrue(str(calls[0][1]["cycle_id"]).startswith("failsafe-"))

    def test_july_amd_pending_sell_sequence_does_not_requeue_or_realert(self):
        app_state["trading_client"] = FakeClient(
            positions=[FakePosition("AMD", 10, avg_entry_price=100.0)]
        )
        app_state["open_trades"]["AMD"] = {
            "status": "filled",
            "buy_price": 100.0,
            "quantity": 10,
        }
        app_state["last_trade_price_by_symbol"]["AMD"] = 90.0
        csv_rows = []
        emails = []

        async def record_email(*args, **kwargs):
            emails.append((args, kwargs))

        with (
            patch.object(fail_safes, "get_config", return_value=5.0),
            patch.object(
                fail_safes,
                "log_trade_to_csv",
                side_effect=lambda *args, **kwargs: csv_rows.append((args, kwargs)),
            ),
            patch.object(fail_safes, "send_fail_safe_alert_async", record_email),
        ):
            asyncio.run(fail_safes.check_per_stock_fail_safe())
            app_state["open_trades"]["AMD"]["status"] = "pending_sell"
            asyncio.run(fail_safes.check_per_stock_fail_safe())

        self.assertEqual(1, len(csv_rows))
        self.assertEqual(1, len(emails))
        self.assertEqual(1, len(app_state["fail_safes"]["lifecycles"]))
        lifecycle.mark_submitted("AMD", FakeOrder(symbol="AMD"))
        lifecycle.record_order_update(
            "AMD",
            FakeOrder(symbol="AMD", status="filled", filled_qty=10),
        )
        app_state["open_trades"].pop("AMD")
        lifecycle.reconcile(positions=[], open_orders=[])
        self.assertFalse(lifecycle.snapshot()["active"])
        self.assertEqual([], app_state["fail_safes"]["pending_liquidation_symbols"])

    def test_broker_average_entry_prevents_stale_local_false_trigger(self):
        app_state["trading_client"] = FakeClient(
            positions=[FakePosition("AMD", 54, avg_entry_price=446.96)]
        )
        app_state["open_trades"]["AMD"] = {
            "status": "filled",
            "buy_price": 500.0,
            "quantity": 54,
        }
        app_state["last_trade_price_by_symbol"]["AMD"] = 461.93

        with patch.object(fail_safes, "get_config", return_value=5.0):
            asyncio.run(fail_safes.check_per_stock_fail_safe())

        self.assertFalse(lifecycle.snapshot()["active"])

    def test_unrelated_order_update_cannot_corrupt_liquidation_lifecycle(self):
        self.queue_amd()
        lifecycle.mark_submitted(
            "AMD",
            FakeOrder(symbol="AMD", id="liquidation-order"),
        )

        lifecycle.record_order_update(
            "AMD",
            FakeOrder(
                symbol="AMD",
                id="strategic-buy-order",
                side="buy",
                status="filled",
                filled_qty=16,
            ),
        )

        state = app_state["fail_safes"]["lifecycles"]["AMD"]
        self.assertEqual("submitted_open", state["lifecycle_state"])
        self.assertEqual("liquidation-order", state["order_id"])
        self.assertEqual(0.0, state["filled_qty"])

    def test_filled_liquidation_with_reacquired_position_becomes_retryable(self):
        self.queue_amd()
        lifecycle.mark_submitted(
            "AMD",
            FakeOrder(symbol="AMD", id="liquidation-order"),
        )
        lifecycle.record_order_update(
            "AMD",
            FakeOrder(
                symbol="AMD",
                id="liquidation-order",
                status="filled",
                filled_qty=54,
            ),
        )

        with patch.object(lifecycle.time, "time", return_value=300.0):
            lifecycle.reconcile(
                positions=[FakePosition("AMD", 16, avg_entry_price=477.46)],
                open_orders=[],
            )

        state = app_state["fail_safes"]["lifecycles"]["AMD"]
        self.assertEqual("waiting_retry", state["lifecycle_state"])
        self.assertEqual(16.0, state["remaining_broker_position_qty"])
        self.assertFalse(lifecycle.eligible_for_submission("AMD", now_epoch=329.9))
        self.assertTrue(lifecycle.eligible_for_submission("AMD", now_epoch=330.0))


if __name__ == "__main__":
    unittest.main()
