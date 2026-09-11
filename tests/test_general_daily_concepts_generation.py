import json
import unittest
from pathlib import Path

from research.tier1_etf_replay import config_from_job
from research.universes import resolve_universe


ROOT = Path(__file__).resolve().parents[1] / "research"


class GeneralDailyConceptGenerationTests(unittest.TestCase):
    def test_generation_is_independent_state_research(self):
        declaration = json.loads(
            (ROOT / "general-daily-concepts-generation-005.json").read_text(encoding="utf-8")
        )
        self.assertTrue(declaration["constraints"]["no_parameter_search"])
        self.assertTrue(declaration["constraints"]["no_strategy_combination"])
        self.assertTrue(declaration["constraints"]["no_router_training"])
        self.assertEqual(
            "persistent causal market-state episode",
            declaration["evaluation_policy"]["primary_unit"],
        )
        self.assertEqual(10, len(declaration["candidate_families"]))

    def test_job_covers_union_universe_and_all_declared_concepts(self):
        job = json.loads(
            (ROOT / "general-daily-concepts-generation-005-job.json").read_text(encoding="utf-8")
        )
        config = config_from_job(job)
        self.assertEqual(37, len(resolve_universe(config.universe_name)))
        self.assertEqual(14, len(config.strategy_names))
        declared = json.loads(
            (ROOT / "general-daily-concepts-generation-005.json").read_text(encoding="utf-8")
        )
        expected = {
            row["strategy"] for row in declared["candidate_families"]
        } | set(declared["controls"])
        self.assertEqual(expected, set(config.strategy_names))
        self.assertEqual("2026-09-03", declared["retrospective_evidence_end"])


if __name__ == "__main__":
    unittest.main()
