import json
import unittest
from pathlib import Path

from research.strategy_registry import validate_experiment_declaration
from research.tier1_etf_replay import config_from_job
from research.universes import resolve_universe


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

    def test_phase_002_direct_job_has_stable_allocations_and_shared_state_roster(self):
        path = ROOT / "long-term-condition-mapping-phase-002-direct-job.json"
        job = json.loads(path.read_text(encoding="utf-8"))
        validate_experiment_declaration(job["experiment"])
        config = config_from_job(job)
        self.assertEqual("ETF_LONG_TERM_STATE_CANONICAL", config.market_state_universe_name)
        self.assertGreater(len(resolve_universe(config.universe_name)), 30)
        self.assertEqual(30, len(resolve_universe("ETF_LONG_TERM_LIVE_30")))
        self.assertTrue(
            set(resolve_universe(config.market_state_universe_name)).issubset(
                resolve_universe(config.universe_name)
            )
        )
        live_symbols = set(resolve_universe("ETF_LONG_TERM_LIVE_30")) - {"SPY"}
        benchmark_symbols = {
            name.removesuffix("_BUY_HOLD") for name in config.strategy_names
            if name.endswith("_BUY_HOLD")
        }
        self.assertTrue(live_symbols.issubset(benchmark_symbols))
        self.assertTrue({"VIG", "SPLV"}.issubset(benchmark_symbols))


if __name__ == "__main__":
    unittest.main()
