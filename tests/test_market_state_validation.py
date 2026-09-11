import unittest

from research.market_state_validation import (
    _benjamini_hochberg, _episode_bootstrap, _fold_recurrence,
    _survival_table, state_validation_outputs,
)


class MarketStateValidationTests(unittest.TestCase):
    def _fixture(self):
        conditions, daily = {}, []
        for index in range(1, 13):
            day = f"2026-01-{index:02d}"
            conditions[day] = {
                "trend_200d_distance": .10, "trend_63d_return": .08,
                "trend_20d_return": .03, "trend_acceleration_5d": .01,
                "volatility_20d_bucket": "Q2", "breadth_50d": .75,
                "volatility_change_5d": -.01, "breadth_50d_change_5d": .02,
                "correlation_20d_bucket": "Q3", "dispersion_20d_bucket": "Q2",
            }
        for cost in (1.0, 10.0):
            for strategy, growth in (("SPY_BUY_HOLD", 1.01), ("CANDIDATE", 1.02)):
                equity = 100.0
                for index in range(1, 13):
                    daily.append({"strategy": strategy, "date": f"2026-01-{index:02d}",
                                  "cost_bps": cost, "equity": equity})
                    equity *= growth
        return daily, conditions

    def test_episode_bootstrap_is_deterministic(self):
        first = _episode_bootstrap([.01, .02, -.01], samples=100)
        self.assertEqual(first, _episode_bootstrap([.01, .02, -.01], samples=100))
        self.assertGreater(first["probability_positive"], .5)

    def test_bh_adjustment_is_monotone_and_in_place(self):
        rows = [{"raw_p_value": value} for value in (.01, .02, .20)]
        _benjamini_hochberg(rows)
        self.assertEqual([.03, .03, .20], [round(row["bh_adjusted_p_value"], 2) for row in rows])

    def test_outputs_keep_period_cost_and_fold_evidence_separate(self):
        daily, conditions = self._fixture()
        (periods, inference, costs, transitions, folds, recurrence,
         transition_horizons, survival) = state_validation_outputs(
            daily, conditions, (1.0, 10.0), 10.0, "2026-01-06", "2026-01-07",
            fold_sessions=6,
        )
        self.assertEqual({"full", "discovery", "holdout"}, {row["period"] for row in periods})
        self.assertTrue(inference)
        self.assertEqual({1.0, 10.0}, {row["cost_bps"] for row in costs})
        self.assertEqual({"full", "discovery", "holdout"}, {row["period"] for row in costs})
        self.assertEqual([], folds)  # tiny folds fail the deliberate 30-session floor
        self.assertEqual([], recurrence)
        self.assertEqual([], transition_horizons)
        self.assertTrue(survival)
        self.assertTrue(any(row.get("session_type") == "stable_state_session"
                            for row in transitions))

    def test_fold_recurrence_requires_three_folds_and_sixty_percent_wins(self):
        rows = [{"strategy": "CANDIDATE", "core_state": "STATE", "sample_sufficient": True,
                 "relative_wealth_vs_spy": value} for value in (.03, .02, -.01)]
        result = _fold_recurrence(rows, "SPY_BUY_HOLD")[0]
        self.assertTrue(result["recurs_in_chronological_folds"])
        self.assertAlmostEqual(2 / 3, result["fold_win_rate"])

    def test_survival_table_never_authorizes_routing(self):
        key = {"strategy": "CANDIDATE", "core_state": "STATE"}
        periods = [{**key, "period": "holdout", "sample_sufficient": True,
                    "sessions": 50, "episodes": 8, "relative_wealth_vs_spy": .10}]
        inference = [{**key, "period": "holdout", "passes_fdr_05": True,
                      "episode_excess_ci_95_low": .01}]
        costs = [{**key, "period": "holdout", "cost_bps": 20.0,
                  "relative_wealth_vs_spy": .05}]
        recurrence = [{**key, "recurs_in_chronological_folds": True,
                       "eligible_folds": 4, "fold_win_rate": .75}]
        result = _survival_table(periods, inference, costs, recurrence, "SPY_BUY_HOLD")[0]
        self.assertEqual("COST_ROBUST_AWAITING_FORWARD_VALIDATION", result["status"])
        self.assertFalse(result["routing_eligible"])
        self.assertEqual(0, result["forward_observations"])


if __name__ == "__main__":
    unittest.main()
