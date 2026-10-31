import unittest

from research.execution_aware_strategy_audit import _path_metrics, holding_by_date


class ExecutionAwareStrategyAuditTests(unittest.TestCase):
    def test_holding_reconstruction_uses_opening_switch_buys(self):
        daily = [
            {"date": "2026-01-02", "strategy": "S", "cost_bps": "10.0"},
            {"date": "2026-01-05", "strategy": "S", "cost_bps": "10.0"},
            {"date": "2026-01-06", "strategy": "S", "cost_bps": "10.0"},
        ]
        trades = [
            {"date": "2026-01-02", "strategy": "S", "symbol": "SPY", "notional": "100000", "cost_bps": "10.0"},
            {"date": "2026-01-05", "strategy": "S", "symbol": "SPY", "notional": "-99000", "cost_bps": "10.0"},
            {"date": "2026-01-05", "strategy": "S", "symbol": "BIL", "notional": "98900", "cost_bps": "10.0"},
        ]
        self.assertEqual(
            {"2026-01-02": "SPY", "2026-01-05": "BIL", "2026-01-06": "BIL"},
            holding_by_date(daily, trades, "S", 10.0),
        )

    def test_path_metrics_compound_returns_and_drawdown(self):
        metrics = _path_metrics([0.10, -0.10])
        self.assertAlmostEqual(-0.01, metrics["total_return"])
        self.assertAlmostEqual(-0.10, metrics["max_drawdown"])


if __name__ == "__main__":
    unittest.main()
