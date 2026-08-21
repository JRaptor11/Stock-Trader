import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from research.historical_replay import (
    ReplayConfig, _walk_forward_results, load_bar_csv, run_replay, write_replay,
)
from research.replay_parity import compare_replay_to_live
from research.walk_forward import build_walk_forward_folds
from layers.layer_research_strategy import STRATEGIES


class HistoricalReplayTests(unittest.TestCase):
    def _bars(self, count=76):
        start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
        rows = []
        for index in range(count):
            ts = start + timedelta(minutes=5 * index)
            for symbol, offset in (("AAA", 0.0), ("BBB", 10.0)):
                price = 100.0 + offset + index * (0.05 if symbol == "AAA" else -0.02)
                rows.append({
                    "timestamp": ts, "symbol": symbol, "open": price,
                    "high": price + 0.1, "low": price - 0.1, "close": price + 0.02,
                    "volume": 1000 + index, "trade_count": 100 + index, "vwap": price,
                })
        return rows

    def test_replay_generates_stateful_outputs_and_future_labels(self):
        result = run_replay(self._bars(), ReplayConfig(warmup_bars=61, min_train_sessions=1, test_sessions=1, require_benchmark=False))
        self.assertTrue(result["cycles"])
        self.assertTrue(result["orders"])
        self.assertTrue(result["dataset"])
        self.assertIn("forward_return_30m", result["dataset"][0])
        self.assertEqual(
            len({row["strategy_name"] for row in result["cycles"]}),
            len(STRATEGIES),
        )
        first_order = result["orders"][0]
        self.assertGreater(first_order["timestamp"], first_order["source_bar_timestamp"])
        self.assertGreaterEqual(first_order["timestamp"], first_order["decision_available_at"])
        self.assertTrue(result["daily"])

    def test_loader_rejects_duplicate_symbol_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "symbol", "open", "high", "low", "close", "volume"])
                writer.writeheader()
                row = {"timestamp": "2026-01-05T14:30:00+00:00", "symbol": "AAA", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
                writer.writerow(row); writer.writerow(row)
            with self.assertRaises(ValueError):
                load_bar_csv(path)

    def test_loader_rejects_timezone_naive_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "symbol", "open", "high", "low", "close", "volume"])
                writer.writeheader()
                writer.writerow({"timestamp": "2026-01-05T14:30:00", "symbol": "AAA", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})
            with self.assertRaises(ValueError):
                load_bar_csv(path)

    def test_benchmark_is_context_not_candidate_by_default(self):
        rows = self._bars()
        for index in range(76):
            ts = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc) + timedelta(minutes=5 * index)
            rows.append({"timestamp": ts, "symbol": "SPY", "open": 500, "high": 501, "low": 499, "close": 500.5, "volume": 10000, "trade_count": 1000, "vwap": 500})
        result = run_replay(rows, ReplayConfig(warmup_bars=61))
        self.assertEqual(result["symbols"], ["AAA", "BBB"])
        self.assertIn("benchmark_ret_60m", result["dataset"][0])
        self.assertTrue(result["benchmark_daily"])
        self.assertEqual(result["dataset_quality"]["minimum_coverage_pct"], 100.0)

    def test_walk_forward_is_strictly_chronological(self):
        dates = [f"2026-01-{day:02d}" for day in range(1, 11)]
        folds = build_walk_forward_folds(dates, min_train_sessions=4, test_sessions=2, step_sessions=2)
        self.assertEqual(len(folds), 3)
        for fold in folds:
            self.assertLess(fold.train_end, fold.test_start)
            self.assertTrue(set(fold.train_dates).isdisjoint(fold.test_dates))

    def test_walk_forward_selects_on_train_and_scores_held_out_test(self):
        days = ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04")
        folds = build_walk_forward_folds(days, min_train_sessions=2, test_sessions=2)
        daily = []
        for strategy, equities in {
            "TRAIN_WINNER": [100, 110, 99, 89.1],
            "TEST_WINNER": [100, 99, 108.9, 119.79],
        }.items():
            for day, equity in zip(days, equities):
                daily.append({
                    "session_date": day, "strategy_name": strategy,
                    "first_equity": 100, "last_equity": equity,
                })
        benchmark = [{"session_date": day, "session_return": 0.0} for day in days]
        results = _walk_forward_results(daily, benchmark, folds)
        self.assertEqual(results[0]["selected_strategy"], "TRAIN_WINNER")
        self.assertLess(results[0]["selected_test_return"], 0)

    def test_writer_produces_reproducibility_manifest(self):
        result = run_replay(self._bars(), ReplayConfig(warmup_bars=61, require_benchmark=False))
        with tempfile.TemporaryDirectory() as directory:
            output = write_replay(result, directory)
            self.assertTrue((output / "replay_manifest.json").exists())
            self.assertTrue((output / "ml_dataset.csv").exists())
            self.assertTrue((output / "dataset_quality.json").exists())

    def test_replay_requires_benchmark_by_default(self):
        with self.assertRaisesRegex(ValueError, "required benchmark"):
            run_replay(self._bars(), ReplayConfig(warmup_bars=61))

    def test_replay_rejects_materially_incomplete_symbol_coverage(self):
        rows = self._bars()
        rows = [row for row in rows if not (row["symbol"] == "BBB" and row["timestamp"].minute % 10 == 0)]
        with self.assertRaisesRegex(ValueError, "coverage"):
            run_replay(rows, ReplayConfig(warmup_bars=61, require_benchmark=False))

    def test_replay_parity_matches_on_source_bar_and_strategy(self):
        replay = [{
            "timestamp": "2026-01-05T15:00:00+00:00", "strategy_name": "LOOKBACK_60M",
            "equity": 100010, "turnover": 5000, "trade_count": 2, "drawdown_pct": -0.001,
        }]
        live = [{
            "source_bar_timestamp": "2026-01-05T15:00:00+00:00", "strategy_name": "LOOKBACK_60M",
            "shadow_equity": 100000, "cumulative_gross_turnover": 4900,
            "cumulative_trade_count": 2, "drawdown_pct": -0.0012,
        }]
        result = compare_replay_to_live(replay, live)
        self.assertEqual(result["matched_cycle_count"], 1)
        summary = result["strategy_summaries"][0]
        self.assertEqual(summary["equity_final_difference"], 10.0)
        self.assertEqual(summary["turnover_final_difference"], 100.0)


if __name__ == "__main__":
    unittest.main()
