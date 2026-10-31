import unittest

from research.turning_point_cycles import (
    causal_turning_point_labels, turning_point_cycle_scorecards,
)


class TurningPointCycleTests(unittest.TestCase):
    def test_labels_use_only_the_prior_completed_close(self):
        dates = [f"2024-01-{index:02d}" for index in range(1, 10)]
        closes = [100.0, 101.0, 102.0, 110.0, 108.0, 106.0, 90.0, 92.0, 94.0]
        bars = {
            day: {"SPY": {"close": close}}
            for day, close in zip(dates, closes)
        }
        labels = causal_turning_point_labels(
            dates, bars, "SPY", dates[0], slow_lookback=4, fast_lookback=2,
        )
        by_date = {row["date"]: row for row in labels}
        self.assertEqual("BULL", by_date["2024-01-06"]["turning_point_cycle"])
        self.assertEqual("CORRECTION", by_date["2024-01-07"]["turning_point_cycle"])
        self.assertEqual("BEAR", by_date["2024-01-08"]["turning_point_cycle"])

    def test_scorecards_preserve_episode_boundaries_and_relative_wealth(self):
        labels = [
            {"date": "2024-01-02", "turning_point_cycle": "BULL"},
            {"date": "2024-01-03", "turning_point_cycle": "BULL"},
            {"date": "2024-01-04", "turning_point_cycle": "CORRECTION"},
            {"date": "2024-01-05", "turning_point_cycle": "BULL"},
        ]
        daily = []
        spy_equity = candidate_equity = 100.0
        for index, day in enumerate([row["date"] for row in labels]):
            spy_equity *= 1.01
            candidate_equity *= 1.02 if index == 2 else 1.01
            daily.extend([
                {"date": day, "strategy": "SPY_BUY_HOLD", "equity": spy_equity},
                {"date": day, "strategy": "CANDIDATE", "equity": candidate_equity},
            ])
        episodes, attribution, episode_returns = turning_point_cycle_scorecards(
            daily, labels, 100.0, cost_bps=10.0,
        )
        self.assertEqual(3, len(episodes))
        self.assertFalse(episodes[0]["complete_episode"])
        self.assertTrue(episodes[1]["complete_episode"])
        self.assertFalse(episodes[2]["complete_episode"])
        correction = next(
            row for row in attribution
            if row["strategy"] == "CANDIDATE"
            and row["turning_point_cycle"] == "CORRECTION"
        )
        self.assertGreater(correction["relative_wealth_vs_benchmark"], 0.0)
        self.assertEqual(10.0, correction["cost_bps"])
        complete = [
            row for row in episode_returns
            if row["strategy"] == "CANDIDATE" and row["complete_episode"]
        ]
        self.assertEqual(1, len(complete))
        self.assertGreater(complete[0]["relative_wealth_vs_benchmark"], 0.0)


if __name__ == "__main__":
    unittest.main()
