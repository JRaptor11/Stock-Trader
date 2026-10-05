import unittest

from research.downside_event_actionability import build_downside_event_diagnostics


class DownsideEventActionabilityTests(unittest.TestCase):
    def _conditions(self, dates):
        return {day: {
            "trend_200d_distance": -0.05, "trend_20d_return": -0.04,
            "breadth_50d_change_5d": -0.02, "volatility_change_5d": 0.03,
        } for day in dates}

    def test_detects_locked_events_and_clusters_overlapping_sessions(self):
        dates = [f"2026-01-{index + 1:02d}" for index in range(28)]
        equity = [100.0 - index * 0.5 for index in range(28)]
        daily = [{"strategy": "SPY_BUY_HOLD", "date": day,
                  "cost_bps": 10.0, "equity": value}
                 for day, value in zip(dates, equity)]
        sessions, summary, overlap = build_downside_event_diagnostics(
            daily, self._conditions(dates), 10.0,
        )
        event_types = {row["event_type"] for row in sessions}
        self.assertIn("FRESH_20D_LOW", event_types)
        self.assertIn("BREADTH_VOL_JOINT_DOWNSIDE", event_types)
        self.assertTrue(summary)
        self.assertTrue(overlap)
        fresh_clusters = {row["event_cluster_id"] for row in sessions
                          if row["event_type"] == "FRESH_20D_LOW"}
        self.assertEqual(1, len(fresh_clusters))
        self.assertTrue(all(row["diagnostic_only"] for row in sessions))

    def test_future_outcomes_do_not_change_event_detection(self):
        dates = [f"2026-02-{index + 1:02d}" for index in range(30)]
        prefix = [100.0 - index * 0.4 for index in range(25)]
        left_values = prefix + [89.0, 88.0, 87.0, 86.0, 85.0]
        right_values = prefix + [95.0, 96.0, 97.0, 98.0, 99.0]
        def rows(values):
            return [{"strategy": "SPY_BUY_HOLD", "date": day,
                     "cost_bps": 10.0, "equity": value}
                    for day, value in zip(dates, values)]
        left, _, _ = build_downside_event_diagnostics(
            rows(left_values), self._conditions(dates), 10.0,
        )
        right, _, _ = build_downside_event_diagnostics(
            rows(right_values), self._conditions(dates), 10.0,
        )
        cutoff = dates[25]
        left_events = [(row["date"], row["event_type"]) for row in left if row["date"] <= cutoff]
        right_events = [(row["date"], row["event_type"]) for row in right if row["date"] <= cutoff]
        self.assertEqual(left_events, right_events)


if __name__ == "__main__":
    unittest.main()
