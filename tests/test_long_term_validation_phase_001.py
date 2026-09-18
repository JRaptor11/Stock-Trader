import json
import unittest
from pathlib import Path

from research.strategy_registry import validate_experiment_declaration
from research.tier1_etf_replay import config_from_job


ROOT = Path(__file__).resolve().parents[1] / "research"
JOBS = tuple(sorted(ROOT.glob("long-term-validation-phase-001-*-job.json")))


class LongTermValidationPhase001Tests(unittest.TestCase):
    def test_cohorts_are_frozen_disjoint_strategy_partitions_with_spy_controls(self):
        expected = {
            "SPY_BUY_HOLD", "GLOBAL_EQUITY_STATIC", "STATIC_60_30_10",
            "CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE", "CROSS_ASSET_DUAL_MOMENTUM",
            "DIVERSIFIED_TREND", "VOL_MANAGED_SPY", "INVERSE_VOLATILITY_BALANCED",
            "STATIC_INFLATION_AWARE", "VALUE_QUALITY_STATIC", "MULTIFACTOR_STATIC",
            "LOW_VOLATILITY_EQUITY", "INDUSTRY_ETF_MOMENTUM", "SECTOR_ETF_ROTATION",
        }
        seen = set()
        self.assertEqual(4, len(JOBS))
        for path in JOBS:
            job = json.loads(path.read_text(encoding="utf-8"))
            validate_experiment_declaration(job["experiment"])
            config = config_from_job(job)
            self.assertIn("SPY_BUY_HOLD", config.strategy_names)
            self.assertEqual(756, config.rolling_window_sessions)
            self.assertEqual(126, config.walk_forward_test_sessions)
            self.assertEqual(126, config.walk_forward_step_sessions)
            challengers = set(config.strategy_names) - {"SPY_BUY_HOLD"}
            self.assertFalse(seen & challengers)
            seen |= challengers
        self.assertEqual(expected - {"SPY_BUY_HOLD"}, seen)


if __name__ == "__main__":
    unittest.main()
