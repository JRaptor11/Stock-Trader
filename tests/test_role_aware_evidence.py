import unittest

from research.role_aware_evidence import build_role_aware_evidence, strategy_role


class Config:
    primary_cost_bps = 10.0
    strategy_names = ("SPY_BUY_HOLD", "BASELINE", "DONCHIAN_TREND_BREAKOUT")


class RoleAwareEvidenceTests(unittest.TestCase):
    def test_roles_keep_baselines_and_tactical_concepts_distinct(self):
        self.assertEqual("benchmark", strategy_role("SPY_BUY_HOLD"))
        self.assertEqual("baseline_candidate", strategy_role("ETF_DUAL_MOMENTUM"))
        self.assertEqual("defensive_override", strategy_role("DRAWDOWN_BRAKE"))
        self.assertEqual("tactical_opportunity", strategy_role("DONCHIAN_TREND_BREAKOUT"))

    def test_tactical_events_are_compared_with_displaced_baseline(self):
        scorecards = [{"strategy": s, "cost_bps": 10, "sharpe": 1, "max_drawdown": -.1,
                       "turnover": 1, "trade_count": 1} for s in Config.strategy_names]
        periods = [{"strategy": s, "cost_bps": 10, "period": p, "total_return": .1}
                   for s in Config.strategy_names for p in ("full", "holdout")]
        daily = []
        for index in range(7):
            daily.append({"strategy": "BASELINE", "cost_bps": 10,
                          "date": f"2025-01-{index+1:02d}", "equity": 100 + index})
        events = [{"strategy": "DONCHIAN_TREND_BREAKOUT", "cost_bps": 10,
                   "entry_date": "2025-01-01", "exit_5d_return": .10}]
        labels = [{"date": "2025-01-01", "core_state": "BULL"}]
        matrix, comparisons, summary = build_role_aware_evidence(
            Config, scorecards, periods, [], [], events, labels, daily, {}, "BASELINE"
        )
        self.assertEqual(3, len(matrix))
        self.assertEqual(1, len(comparisons))
        self.assertGreater(comparisons[0]["incremental_return_vs_baseline"], 0)
        self.assertEqual({"ALL", "BULL"}, {row["core_state"] for row in summary})


if __name__ == "__main__":
    unittest.main()
