import unittest

from research.market_state_validation import _benjamini_hochberg, _episode_bootstrap, state_validation_outputs


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
        periods, inference, costs, transitions, folds = state_validation_outputs(
            daily, conditions, (1.0, 10.0), 10.0, "2026-01-06", "2026-01-07",
            fold_sessions=6,
        )
        self.assertEqual({"full", "discovery", "holdout"}, {row["period"] for row in periods})
        self.assertTrue(inference)
        self.assertEqual({1.0, 10.0}, {row["cost_bps"] for row in costs})
        self.assertEqual([], folds)  # tiny folds fail the deliberate 30-session floor
        self.assertTrue(any(row.get("session_type") == "stable_state_session"
                            for row in transitions))


if __name__ == "__main__":
    unittest.main()
