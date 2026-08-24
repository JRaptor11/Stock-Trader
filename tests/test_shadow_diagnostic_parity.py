from enum import Enum
import sys
import types
import unittest


class _Enum(Enum):
    BUY = "buy"
    SELL = "sell"
    OPEN = "open"
    DAY = "day"


class _Request:
    def __init__(self, *args, **kwargs):
        self.__dict__.update(kwargs)


alpaca = types.ModuleType("alpaca")
alpaca_trading = types.ModuleType("alpaca.trading")
alpaca_enums = types.ModuleType("alpaca.trading.enums")
alpaca_requests = types.ModuleType("alpaca.trading.requests")
alpaca_enums.OrderSide = _Enum
alpaca_enums.QueryOrderStatus = _Enum
alpaca_enums.TimeInForce = _Enum
alpaca_requests.GetOrdersRequest = _Request
alpaca_requests.MarketOrderRequest = _Request
alpaca_requests.LimitOrderRequest = _Request
sys.modules.setdefault("alpaca", alpaca)
sys.modules.setdefault("alpaca.trading", alpaca_trading)
sys.modules.setdefault("alpaca.trading.enums", alpaca_enums)
sys.modules.setdefault("alpaca.trading.requests", alpaca_requests)

from core.state import app_state
from layers.layer4_executor import _update_layer4_shadow_lifecycle
from layers.layer_research_strategy import (
    STRATEGIES,
    _raw_research_target,
    _smooth_target,
)


class _Ranked:
    def __init__(self, symbol, score, last_price=100.0):
        self.symbol = symbol
        self.score = score
        self.last_price = last_price


class ShadowDiagnosticParityTests(unittest.TestCase):
    def setUp(self):
        app_state.setdefault("layers", {}).pop("layer4_shadow", None)

    def test_delayed_order_resolves_at_later_price(self):
        delayed = [{
            "symbol": "AMD", "row_id": "p1:AMD", "would_delay": True,
            "shadow_action": "delay_buy_weak_confirmation",
            "shadow_reason": "weak", "qty": 10, "live_price": 100.0,
        }]
        created = _update_layer4_shadow_lifecycle(delayed, cycle_id=1, plan_id="p1")
        self.assertEqual(created[-1]["event"], "delay_created")

        confirmed = [{
            "symbol": "AMD", "row_id": "p2:AMD", "would_delay": False,
            "shadow_action": "execute_buy_confirmed",
            "shadow_reason": "confirmed", "qty": 10, "live_price": 98.0,
        }]
        resolved = _update_layer4_shadow_lifecycle(confirmed, cycle_id=2, plan_id="p2")
        self.assertEqual(resolved[0]["event"], "delay_executed_later")
        self.assertEqual(resolved[0]["estimated_entry_improvement"], 20.0)
        self.assertEqual(
            app_state["layers"]["layer4_shadow"]["pending_delay_count"], 0
        )

    def test_redesign_can_hold_all_cash_when_nothing_qualifies(self):
        bars = {"AMD": [{"close": 100 - index * 0.1} for index in range(61)]}
        target, decisions = _raw_research_target([_Ranked("AMD", -0.01)], bars)
        self.assertEqual(target["CASH"], 1.0)
        self.assertFalse(decisions[0]["qualified"])

    def test_redesign_rejects_severe_recent_reversal(self):
        closes = [100 + index * 0.2 for index in range(49)]
        closes.extend([109.6 - index * 0.2 for index in range(12)])
        bars = {"AMD": [{"close": close} for close in closes]}
        target, decisions = _raw_research_target([_Ranked("AMD", 0.02)], bars)
        self.assertEqual(target["CASH"], 1.0)
        self.assertTrue(decisions[0]["severe_deterioration"])

    def test_conservative_smoothing_moves_less_than_responsive(self):
        raw = {"AMD": 0.30, "CASH": 0.70, "_meta": {}}
        previous = {"CASH": 1.0}
        conservative = _smooth_target(raw, previous, {"alpha": 0.20, "max_step": 0.035})
        responsive = _smooth_target(raw, previous, {"alpha": 0.40, "max_step": 0.075})
        self.assertLess(conservative["AMD"], responsive["AMD"])

    def test_shorter_lookback_rejects_stale_five_hour_momentum(self):
        closes = [100 + index * 0.2 for index in range(49)]
        # The long trend remains strongly positive while the last hour turns
        # modestly negative (not negative enough to invoke the emergency
        # deterioration rejection shared by every variant).
        closes.extend([109.6 - index * 0.03 for index in range(12)])
        bars = {"AMD": [{"close": close} for close in closes]}
        ranked = [_Ranked("AMD", 0.02)]

        target_300, decision_300 = _raw_research_target(
            ranked, bars, STRATEGIES["LOOKBACK_300M"],
        )
        target_60, decision_60 = _raw_research_target(
            ranked, bars, STRATEGIES["LOOKBACK_60M"],
        )

        self.assertGreater(decision_300[0]["ret_300m"], 0)
        self.assertLess(decision_60[0]["ret_60m"], 0)
        self.assertGreater(target_300.get("AMD", 0), 0)
        self.assertEqual(target_60["CASH"], 1.0)

    def test_timing_variants_share_rebalance_settings(self):
        names = [
            "LOOKBACK_60M", "LOOKBACK_150M", "LOOKBACK_300M",
            "MULTI_HORIZON_BLEND", "ADAPTIVE_REVERSAL",
        ]
        settings = {
            (STRATEGIES[name]["alpha"], STRATEGIES[name]["max_step"])
            for name in names
        }
        self.assertEqual(settings, {(0.40, 0.075)})

    def test_retired_discovery_variants_are_not_in_active_shadow_set(self):
        retired = {
            "OVERNIGHT_REGIME_300M_EARLY_ENTRY", "MEAN_REVERSION_30M",
            "LOOKBACK_60M_MODERATE", "LOOKBACK_150M_MODERATE",
            "LOOKBACK_300M_MODERATE", "MULTI_HORIZON_BLEND_MODERATE",
            "ADAPTIVE_REVERSAL_MODERATE",
        }
        self.assertTrue(retired.isdisjoint(STRATEGIES))

    def test_live_conservative_confirmation_is_live_only(self):
        config = STRATEGIES["LIVE_CONSERVATIVE_CONFIRMATION"]
        self.assertEqual(("LIVE",), config["allowed_sources"])
        self.assertEqual("blend", config["target_mode"])
        self.assertEqual(0.20, config["alpha"])
        self.assertEqual(0.035, config["max_step"])


if __name__ == "__main__":
    unittest.main()
