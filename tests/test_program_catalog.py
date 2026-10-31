import json
import tempfile
import unittest
from pathlib import Path

from research.program_catalog import build_program_catalog


class ProgramCatalogTests(unittest.TestCase):
    def test_catalog_is_deterministic_and_marks_prehistory_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            research = root / "research"
            research.mkdir()
            job = {
                "job_id": "history-001",
                "engine": "tier1_etf_daily",
                "bars_csv": "history.csv",
                "experiment": {
                    "hypothesis_id": "FAMILY_001", "trial_id": "history",
                },
                "tier1_config": {
                    "universe_name": "TEST",
                    "strategy_names": ["SPY_BUY_HOLD", "CANDIDATE"],
                    "required_common_start_date": "2013-01-02",
                    "discovery_end_date": "2024-12-31",
                    "holdout_start_date": "2025-01-01",
                    "cost_ladder_bps": [10.0, 20.0],
                    "primary_cost_bps": 10.0,
                },
            }
            (research / "history-job.json").write_text(json.dumps(job), encoding="utf-8")
            (research / "job.example.json").write_text(json.dumps(job), encoding="utf-8")
            first = build_program_catalog(
                root, as_of_date="2026-10-31", consumed_through="2026-10-30",
            )
            second = build_program_catalog(
                root, as_of_date="2026-10-31", consumed_through="2026-10-30",
            )
            self.assertEqual(first, second)
            self.assertEqual(1, first["family_count"])
            self.assertEqual(1, first["job_count"])
            family = first["families"][0]
            self.assertTrue(family["contains_unenumerated_jobs"])
            self.assertEqual("retrospective_consumed", family["jobs"][0]["evidence_class"])
            self.assertTrue(first["policy"]["historical_results_already_viewed_are_not_untouched"])

    def test_forward_start_classifies_prospective_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            research = root / "research"
            research.mkdir()
            job = {
                "job_id": "forward-001",
                "engine": "tier1_etf_daily",
                "experiment": {
                    "hypothesis_id": "FORWARD_FAMILY", "trial_id": "prospective",
                },
                "research_evaluation": {"forward_start_date": "2026-10-30"},
                "tier1_config": {"strategy_names": ["SPY_BUY_HOLD"]},
            }
            (research / "forward-job.json").write_text(json.dumps(job), encoding="utf-8")
            result = build_program_catalog(
                root, as_of_date="2026-10-31", consumed_through="2026-10-30",
            )
            self.assertEqual(
                "prospective_forward", result["families"][0]["jobs"][0]["evidence_class"],
            )


if __name__ == "__main__":
    unittest.main()
