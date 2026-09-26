import json
import unittest
from pathlib import Path

from research.strategy_registry import validate_experiment_declaration
from research.tier1_etf_replay import config_from_job
from research.universes import resolve_universe


ROOT = Path(__file__).resolve().parents[1] / "research"
JOBS = tuple(sorted(ROOT.glob("long-term-validation-phase-001-*-job.json")))
CANONICAL_JOBS = tuple(sorted(ROOT.glob("long-term-canonical-state-validation-003-*-job.json")))
HIERARCHY_JOBS = tuple(sorted(ROOT.glob("long-term-hierarchical-validation-004-*-job.json")))
LOCKED_JOBS = tuple(sorted(ROOT.glob("long-term-locked-validation-005-*-job.json")))


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

    def test_phase_003_preserves_phase_001_families_with_canonical_states(self):
        phase_001 = {}
        for path in JOBS:
            job = json.loads(path.read_text(encoding="utf-8"))
            phase_001[job["research_evaluation"]["cohort"]] = config_from_job(job)

        self.assertEqual(4, len(CANONICAL_JOBS))
        self.assertEqual(set(phase_001), {
            "cross_asset_and_strategic_allocation", "factor_equity",
            "industry_momentum", "sector_momentum",
        })
        for path in CANONICAL_JOBS:
            job = json.loads(path.read_text(encoding="utf-8"))
            declaration = validate_experiment_declaration(job["experiment"])
            self.assertEqual(
                "LONG_TERM_CANONICAL_STATE_VALIDATION_003",
                declaration["hypothesis_id"],
            )
            config = config_from_job(job)
            cohort = job["research_evaluation"]["cohort"]
            original = phase_001[cohort]
            self.assertEqual(original.universe_name, config.universe_name)
            self.assertEqual(original.strategy_names, config.strategy_names)
            self.assertEqual(
                original.required_common_start_date,
                config.required_common_start_date,
            )
            self.assertEqual(
                "ETF_LONG_TERM_STATE_CANONICAL",
                config.market_state_universe_name,
            )
            self.assertTrue(
                set(resolve_universe(config.market_state_universe_name)).issubset(
                    resolve_universe(config.universe_name)
                )
            )

    def test_phase_004_is_locked_hierarchical_non_routing_shortlist(self):
        expected = {
            "SPY_BUY_HOLD", "CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE",
            "STATIC_60_30_10", "STATIC_INFLATION_AWARE",
            "VALUE_QUALITY_STATIC", "MULTIFACTOR_STATIC",
            "LOW_VOLATILITY_EQUITY", "INDUSTRY_ETF_MOMENTUM",
            "SECTOR_ETF_ROTATION",
        }
        seen = set()
        self.assertEqual(4, len(HIERARCHY_JOBS))
        for path in HIERARCHY_JOBS:
            job = json.loads(path.read_text(encoding="utf-8"))
            declaration = validate_experiment_declaration(job["experiment"])
            self.assertEqual(
                "LONG_TERM_HIERARCHICAL_VALIDATION_004",
                declaration["hypothesis_id"],
            )
            self.assertFalse(job["research_evaluation"]["router"])
            config = config_from_job(job)
            self.assertTrue(config.hierarchical_state_validation)
            self.assertEqual(
                "ETF_LONG_TERM_STATE_CANONICAL",
                config.market_state_universe_name,
            )
            seen.update(config.strategy_names)
        self.assertEqual(expected, seen)

    def test_phase_005_freezes_four_hypotheses_for_source_and_forward_validation(self):
        study = json.loads(
            (ROOT / "long-term-locked-validation-005.json").read_text(encoding="utf-8")
        )
        self.assertEqual("paused_and_unchanged", study["generation_015_status"])
        self.assertEqual("2026-09-28", study["design"]["independent_forward_start"])
        self.assertEqual(4, len(study["locked_hypotheses"]))
        self.assertIn("not an independent market sample", study["design"]["source_replication_scope"])

        expected = {
            ("CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE", "trend_breadth", "BULL_DECELERATING__BROAD_BREADTH"),
            ("VALUE_QUALITY_STATIC", "trend", "BULL_ACCELERATING"),
            ("INDUSTRY_ETF_MOMENTUM", "trend_volatility", "BULL_DECELERATING__HIGH_VOL"),
            ("MULTIFACTOR_STATIC", "trend_volatility", "BULL_ACCELERATING__LOW_VOL_OR_NORMAL_VOL"),
        }
        actual = set()
        self.assertEqual(3, len(LOCKED_JOBS))
        for path in LOCKED_JOBS:
            job = json.loads(path.read_text(encoding="utf-8"))
            declaration = validate_experiment_declaration(job["experiment"])
            self.assertEqual("LONG_TERM_LOCKED_VALIDATION_005", declaration["hypothesis_id"])
            self.assertFalse(job["research_evaluation"]["router"])
            self.assertEqual("2026-09-28", job["research_evaluation"]["forward_start_date"])
            config = config_from_job(job)
            self.assertTrue(config.hierarchical_state_validation)
            self.assertEqual(10.0, config.primary_cost_bps)
            self.assertEqual((1.0, 5.0, 10.0, 20.0), config.cost_ladder_bps)
            self.assertEqual("ETF_LONG_TERM_STATE_CANONICAL", config.market_state_universe_name)
            for hypothesis in job["research_evaluation"]["locked_market_state_hypotheses"]:
                actual.add((hypothesis["strategy"], hypothesis["state_level"], hypothesis["market_state"]))
        self.assertEqual(expected, actual)


if __name__ == "__main__":
    unittest.main()
