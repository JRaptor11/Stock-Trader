import unittest

from research.market_conditions import causal_market_conditions, condition_scorecards


class MarketConditionTests(unittest.TestCase):
    def test_features_for_session_use_only_prior_bars(self):
        dates = [f"2026-01-{day:02d}" for day in range(1, 8)]
        bars = {
            day: {
                "SPY": {"open": 99.0 + index, "close": 100.0 + index},
                "XLK": {"open": 49.0 + index, "close": 50.0 + index},
            }
            for index, day in enumerate(dates)
        }
        first = causal_market_conditions(dates, bars, ("SPY", "XLK"), minimum_bucket_history=1)
        changed = {day: {symbol: dict(values) for symbol, values in rows.items()} for day, rows in bars.items()}
        changed[dates[-1]]["SPY"]["open"] = 1000.0
        changed[dates[-1]]["SPY"]["close"] = 1000.0
        second = causal_market_conditions(dates, changed, ("SPY", "XLK"), minimum_bucket_history=1)
        self.assertEqual(first[dates[-1]], second[dates[-1]])

    def test_expanding_buckets_do_not_use_future_observations(self):
        dates = [f"2026-02-{day:02d}" for day in range(1, 10)]
        bars = {
            day: {
                "SPY": {"open": 100.0 + index, "close": 100.0 + index},
                "XLK": {"open": 50.0 + index, "close": 50.0 + index},
            }
            for index, day in enumerate(dates)
        }
        short = causal_market_conditions(dates[:7], bars, ("SPY", "XLK"), minimum_bucket_history=1)
        long = causal_market_conditions(dates, bars, ("SPY", "XLK"), minimum_bucket_history=1)
        self.assertEqual(short[dates[6]], long[dates[6]])

    def test_scorecards_report_distributions_and_sparse_samples(self):
        daily = [
            {"date": f"2026-03-0{index}", "strategy": "TEST", "cost_bps": 10.0, "equity": equity}
            for index, equity in enumerate((100.0, 102.0, 101.0, 104.0), 1)
        ]
        conditions = {
            row["date"]: {
                "volatility_20d_bucket": "Q5_HIGH",
                "trend_200d_distance_bucket": "Q4",
            }
            for row in daily
        }
        singles, pairs = condition_scorecards(
            daily, conditions, 10.0, minimum_samples=5,
            pair_dimensions=(("trend_200d_distance_bucket", "volatility_20d_bucket"),),
        )
        volatility = next(row for row in singles if row["dimension"] == "volatility_20d_bucket")
        self.assertEqual(3, volatility["sessions"])
        self.assertFalse(volatility["sample_sufficient"])
        self.assertIn("median_return", volatility)
        self.assertIn("p05_return", volatility)
        self.assertEqual(1, len(pairs))


if __name__ == "__main__":
    unittest.main()
