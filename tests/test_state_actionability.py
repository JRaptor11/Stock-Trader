import unittest

from research.state_actionability import build_state_actionability_diagnostics


class StateActionabilityTests(unittest.TestCase):
    def test_separates_causal_phases_and_marks_hindsight_fields(self):
        conditions = {}
        daily = []
        equity = 100.0
        for index in range(12):
            day = f"2026-01-{index + 1:02d}"
            bearish = index >= 2
            conditions[day] = {
                "trend_200d_distance": -0.05 if bearish else 0.05,
                "trend_63d_return": -0.04 if bearish else 0.04,
                "trend_20d_return": -0.02 if bearish else 0.02,
                "trend_acceleration_5d": -0.01 if bearish else 0.01,
                "volatility_20d_bucket": "Q4",
                "breadth_50d": 0.30,
                "volatility_change_5d": 0.01,
                "breadth_50d_change_5d": -0.02,
            }
            daily.append({"strategy": "SPY_BUY_HOLD", "date": day,
                          "cost_bps": 10.0, "equity": equity})
            equity *= 0.99 if bearish else 1.01

        sessions, summary, segments = build_state_actionability_diagnostics(
            daily, conditions, 10.0,
        )

        phases = {row["phase"] for row in sessions}
        self.assertIn("RAW_ONSET_UNCONFIRMED", phases)
        self.assertIn("CONFIRMED_ENTRY", phases)
        self.assertIn("CONFIRMED_EARLY", phases)
        self.assertIn("CONFIRMED_PERSISTENT", phases)
        self.assertTrue(all(row["diagnostic_only"] for row in sessions))
        self.assertTrue(all(row["episode_end_is_hindsight_only"] for row in sessions))
        self.assertTrue(summary)
        self.assertTrue({"ENTRY", "EARLY", "PERSISTENCE"}.issubset(
            {row["segment"] for row in segments}
        ))

    def test_forward_outcomes_do_not_change_causal_modifier_buckets(self):
        conditions = {}
        for index in range(10):
            day = f"2026-02-{index + 1:02d}"
            conditions[day] = {
                "trend_200d_distance": -0.05, "trend_63d_return": -0.04,
                "trend_20d_return": -0.02, "trend_acceleration_5d": -0.01,
                "volatility_20d_bucket": "Q4", "breadth_50d": 0.30,
                "volatility_change_5d": 0.01, "breadth_50d_change_5d": -0.02,
            }
        first, second = [], []
        for index in range(10):
            day = f"2026-02-{index + 1:02d}"
            first.append({"strategy": "SPY_BUY_HOLD", "date": day,
                          "cost_bps": 10.0, "equity": 100 + index})
            second.append({"strategy": "SPY_BUY_HOLD", "date": day,
                           "cost_bps": 10.0, "equity": 100 - index})
        left, _, _ = build_state_actionability_diagnostics(first, conditions, 10.0)
        right, _, _ = build_state_actionability_diagnostics(second, conditions, 10.0)
        causal_fields = ("phase", "trend_20d_direction", "trend_acceleration_direction",
                         "volatility_change_direction", "breadth_change_direction")
        self.assertEqual(
            [{key: row[key] for key in causal_fields} for row in left],
            [{key: row[key] for key in causal_fields} for row in right],
        )


if __name__ == "__main__":
    unittest.main()
