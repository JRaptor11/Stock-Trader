import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from research.turning_point_cycle_validation import analyze


def _csv(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


class TurningPointCycleValidationTests(unittest.TestCase):
    def test_combines_complete_episodes_and_requires_enabled_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive.zip"
            scorecards = []
            for strategy, value in (("SPY_BUY_HOLD", 0.10), ("TURNING_POINT_SLOW_EQUITY", 0.20)):
                scorecards.append({
                    "strategy": strategy, "cost_bps": 10, "total_return": value,
                    "cagr": value, "annualized_volatility": 0.1, "sharpe": 1,
                    "max_drawdown": -0.1, "turnover": 1, "trade_count": 2,
                })
            episodes = [
                {"strategy": "TURNING_POINT_SLOW_EQUITY", "benchmark": "SPY_BUY_HOLD",
                 "cost_bps": 10, "episode_number": 1, "turning_point_cycle": "BEAR",
                 "episode_start": "2020-01-01", "episode_end": "2020-01-02",
                 "sessions": 2, "complete_episode": True, "strategy_return": 0.01,
                 "benchmark_return": -0.01, "relative_wealth_vs_benchmark": 0.02},
            ]
            states = [
                {"period": "full", "strategy": "TURNING_POINT_SLOW_EQUITY",
                 "core_state": "BEAR__HIGH__NARROW", "trend_state": "BEAR",
                 "cost_bps": 10, "sessions": 20, "episodes": 2,
                 "sample_sufficient": True, "relative_wealth_vs_spy": 0.03,
                 "episode_beat_spy_rate": 0.5, "conditional_sequence_max_drawdown": -0.05},
            ]
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("tier1_manifest.json", json.dumps({
                    "source_sha256": "abc", "coverage": {"scored_start": "2020-01-01",
                    "scored_end": "2020-12-31", "scored_sessions": 252},
                    "config": {"initial_cash": 100.0},
                    "turning_point_cycle_diagnostics": {"enabled": True},
                }))
                bundle.writestr("tier1_cost_ladder_scorecard.csv", _csv(scorecards))
                bundle.writestr("tier1_turning_point_cycle_episode_returns.csv", _csv(episodes))
                bundle.writestr("tier1_turning_point_cycle_labels.csv", _csv([
                    {"date": "2020-01-01", "turning_point_cycle": "BEAR"},
                    {"date": "2020-01-02", "turning_point_cycle": "BEAR"},
                ]))
                bundle.writestr("tier1_daily.csv", _csv([
                    {"date": "2020-01-01", "strategy": "SPY_BUY_HOLD", "cost_bps": 10, "equity": 99},
                    {"date": "2020-01-02", "strategy": "SPY_BUY_HOLD", "cost_bps": 10, "equity": 100},
                    {"date": "2020-01-01", "strategy": "TURNING_POINT_SLOW_EQUITY", "cost_bps": 10, "equity": 100},
                    {"date": "2020-01-02", "strategy": "TURNING_POINT_SLOW_EQUITY", "cost_bps": 10, "equity": 102},
                ]))
                hierarchy_states = [{
                    "state_level": "trend", "state_key": "BEAR", **states[0],
                }]
                bundle.writestr("tier1_market_state_hierarchy_period_scorecard.csv", _csv(hierarchy_states))
            payload = analyze({"era": archive}, root / "out")
            self.assertEqual("descriptive_two_era_validation_complete", payload["status"])
            with (root / "out" / "turning_point_cycle_combined_summary.csv").open(
                encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual("1", rows[0]["episodes"])
            self.assertEqual("1", rows[0]["positive_median_sources"])
            with (root / "out" / "turning_point_cycle_block_summary.csv").open(
                encoding="utf-8"
            ) as handle:
                blocks = list(csv.DictReader(handle))
            candidate = next(row for row in blocks if row["strategy"].startswith("TURNING_POINT_"))
            self.assertEqual("1", candidate["positive_blocks"])


if __name__ == "__main__":
    unittest.main()
