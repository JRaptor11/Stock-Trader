import sys
import types
import unittest


alpaca = types.ModuleType("alpaca")
alpaca_trading = types.ModuleType("alpaca.trading")
alpaca_enums = types.ModuleType("alpaca.trading.enums")
alpaca_requests = types.ModuleType("alpaca.trading.requests")
alpaca_enums.QueryOrderStatus = types.SimpleNamespace(OPEN="open")
alpaca_requests.GetOrdersRequest = lambda **kwargs: kwargs
sys.modules.setdefault("alpaca", alpaca)
sys.modules.setdefault("alpaca.trading", alpaca_trading)
sys.modules.setdefault("alpaca.trading.enums", alpaca_enums)
sys.modules.setdefault("alpaca.trading.requests", alpaca_requests)

from core.state import app_state
from trading.portfolio_reconciler import (
    _sync_broker_positions_to_open_trades,
    _update_position_disappearance_quarantine,
)


class PositionDisappearanceQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.old_open_trades = app_state.get("open_trades")
        self.old_reconcile = app_state.get("portfolio_reconcile")
        app_state["open_trades"] = {
            "GOOGL": {
                "status": "filled", "quantity": 29.0,
                "buy_price": 340.0,
            }
        }
        app_state["portfolio_reconcile"] = {}

    def tearDown(self):
        if self.old_open_trades is None:
            app_state.pop("open_trades", None)
        else:
            app_state["open_trades"] = self.old_open_trades
        if self.old_reconcile is None:
            app_state.pop("portfolio_reconcile", None)
        else:
            app_state["portfolio_reconcile"] = self.old_reconcile

    def test_unexplained_missing_position_blocks_and_is_not_removed(self):
        prior = {"GOOGL": {"symbol": "GOOGL", "qty": 29.0}}
        quarantine = _update_position_disappearance_quarantine(
            previous_positions=prior,
            broker_positions={},
            broker_open_orders_by_symbol={},
        )
        self.assertTrue(quarantine["execution_blocked"])
        self.assertEqual(["GOOGL"], quarantine["symbols"])

        mismatches, repairs = _sync_broker_positions_to_open_trades(
            broker_positions={}, broker_open_orders_by_symbol={},
            quarantined_symbols={"GOOGL"}, repair=True,
        )
        self.assertIn("GOOGL", app_state["open_trades"])
        self.assertEqual([], repairs)
        self.assertEqual(
            "broker_position_disappearance_quarantined",
            mismatches[0]["type"],
        )

    def test_second_missing_snapshot_confirms_and_reappearance_clears(self):
        prior = {"GOOGL": {"symbol": "GOOGL", "qty": 29.0}}
        _update_position_disappearance_quarantine(
            previous_positions=prior, broker_positions={},
            broker_open_orders_by_symbol={},
        )
        quarantine = _update_position_disappearance_quarantine(
            previous_positions=prior, broker_positions={},
            broker_open_orders_by_symbol={},
        )
        self.assertEqual(["GOOGL"], quarantine["confirmed_symbols"])
        quarantine = _update_position_disappearance_quarantine(
            previous_positions={},
            broker_positions={"GOOGL": {"symbol": "GOOGL", "qty": 29.0}},
            broker_open_orders_by_symbol={},
        )
        self.assertFalse(quarantine["active"])

    def test_pending_sell_explains_disappearance(self):
        app_state["open_trades"]["GOOGL"]["status"] = "pending_sell"
        quarantine = _update_position_disappearance_quarantine(
            previous_positions={"GOOGL": {"qty": 29.0}},
            broker_positions={}, broker_open_orders_by_symbol={},
        )
        self.assertFalse(quarantine["active"])


if __name__ == "__main__":
    unittest.main()
