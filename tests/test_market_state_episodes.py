import unittest

from research.market_state_episodes import causal_state_labels, market_state_scorecards


class MarketStateEpisodeTests(unittest.TestCase):
    def _conditions(self):
        base = {
            "trend_200d_distance": .10, "trend_63d_return": .08,
            "trend_20d_return": .03, "trend_acceleration_5d": .01,
            "volatility_20d_bucket": "Q2", "breadth_50d": .75,
            "volatility_change_5d": -.01, "breadth_50d_change_5d": .02,
            "correlation_20d_bucket": "Q3", "dispersion_20d_bucket": "Q2",
        }
        return {f"2026-01-0{index}": {**base} for index in range(2, 6)}

    def test_labels_are_interpretable_and_causal(self):
        labels = causal_state_labels(self._conditions())
        row = labels["2026-01-02"]
        self.assertEqual("BULL_ACCELERATING", row["trend_state"])
        self.assertEqual("LOW", row["volatility_state"])
        self.assertEqual("BROAD", row["breadth_state"])
        self.assertEqual("FALLING", row["volatility_transition"])
        self.assertEqual(row["raw_core_state"], row["core_state"])

    def test_state_change_requires_three_observed_sessions(self):
        conditions = self._conditions()
        template = conditions["2026-01-02"]
        for index in range(6, 10):
            conditions[f"2026-01-0{index}"] = {
                **template, "volatility_20d_bucket": "Q5_HIGH",
            }
        labels = causal_state_labels(conditions)
        self.assertEqual("LOW", labels["2026-01-06"]["volatility_state"])
        self.assertEqual("LOW", labels["2026-01-07"]["volatility_state"])
        self.assertEqual("HIGH", labels["2026-01-08"]["volatility_state"])
        self.assertTrue(labels["2026-01-08"]["state_changed"])

    def test_pools_noncontiguous_state_episodes(self):
        conditions = self._conditions()
        labels = causal_state_labels(conditions, confirmation_sessions=1)
        labels["2026-01-03"]["core_state"] = "ALTERNATE"
        from research.market_state_episodes import state_episodes
        episodes, _ = state_episodes(labels)
        daily = []
        for strategy, equities in {
            "SPY_BUY_HOLD": [100, 101, 100, 102, 103],
            "CANDIDATE": [100, 102, 101, 104, 106],
        }.items():
            for index, equity in enumerate(equities, 1):
                daily.append({
                    "strategy": strategy, "date": f"2026-01-0{index}",
                    "cost_bps": 10.0, "equity": equity,
                })
        labels, _, attribution, episode_returns = market_state_scorecards(
            daily, conditions, 10.0, minimum_state_sessions=1,
            minimum_state_episodes=1,
        )
        self.assertEqual(4, len(labels))
        self.assertEqual(3, len(episodes))
        self.assertTrue(episode_returns)
        pooled = [row for row in attribution if row.get("strategy") == "CANDIDATE"]
        self.assertEqual(1, len(pooled))
        self.assertGreater(pooled[0]["strategy_compounded_return"], 0)


if __name__ == "__main__":
    unittest.main()
