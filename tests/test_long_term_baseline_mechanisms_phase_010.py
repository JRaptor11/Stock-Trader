import json
import unittest
from pathlib import Path

from research.strategy_registry import validate_experiment_declaration
from research.tier1_etf_replay import STRATEGIES
from research.universes import UNIVERSES


ROOT = Path(__file__).resolve().parents[1]
STUDY_PATH = ROOT / "research" / "long-term-baseline-mechanisms-phase-010.json"
JOB_PATHS = (
    ROOT / "research" / "long-term-baseline-mechanisms-phase-010-prehistory-job.json",
    ROOT / "research" / "long-term-baseline-mechanisms-phase-010-modern-job.json",
)


class LongTermBaselineMechanismsPhase010Tests(unittest.TestCase):
    def test_declaration_is_frozen_and_non_routing(self):
        study = json.loads(STUDY_PATH.read_text(encoding="utf-8"))
        self.assertEqual("locked_before_return_analysis", study["status"])
        self.assertEqual("paused_and_unchanged", study["generation_015_status"])
        self.assertFalse(study["design"]["router"])
        self.assertFalse(study["design"]["parameter_retuning"])
        self.assertEqual("none", study["promotion_effect"])
        self.assertEqual(10, len(study["frozen_roster"]))

    def test_jobs_match_the_frozen_roster_and_supported_universe(self):
        study = json.loads(STUDY_PATH.read_text(encoding="utf-8"))
        expected = [row["strategy"] for row in study["frozen_roster"]]
        for path in JOB_PATHS:
            job = json.loads(path.read_text(encoding="utf-8"))
            declaration = validate_experiment_declaration(job["experiment"])
            self.assertEqual("LONG_TERM_BASELINE_MECHANISMS_PHASE_010", declaration["hypothesis_id"])
            config = job["tier1_config"]
            self.assertEqual(expected, config["strategy_names"])
            self.assertEqual([1.0, 5.0, 10.0, 20.0], config["cost_ladder_bps"])
            self.assertEqual(10.0, config["primary_cost_bps"])
            self.assertTrue(config["hierarchical_state_validation"])
            self.assertEqual("baseline_core", config["diagnostic_profile"])
            self.assertTrue(set(expected).issubset(STRATEGIES))
            self.assertIn(config["universe_name"], UNIVERSES)

    def test_primary_evidence_windows_do_not_overlap(self):
        study = json.loads(STUDY_PATH.read_text(encoding="utf-8"))
        self.assertIn("ending 2010-12-31", study["design"]["non_overlap_rule"])
        self.assertIn("beginning 2011-01-03", study["design"]["non_overlap_rule"])


if __name__ == "__main__":
    unittest.main()
