import json
import unittest
from pathlib import Path

from research.role_aware_evidence import strategy_role


ROOT = Path(__file__).resolve().parents[1] / "research"


class RoleCohortSplitTests(unittest.TestCase):
    def test_cohorts_cover_monolith_without_role_leakage(self):
        mono = json.loads((ROOT / "unified-role-aware-generation-010-job.json").read_text())
        names = set(mono["tier1_config"]["strategy_names"])
        cohort_names = {}
        for cohort in ("baseline", "defensive", "tactical"):
            job = json.loads((ROOT / f"unified-role-aware-generation-010-{cohort}-job.json").read_text())
            cohort_names[cohort] = set(job["tier1_config"]["strategy_names"])
            self.assertIn("SPY_BUY_HOLD", cohort_names[cohort])
            self.assertIn("CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE", cohort_names[cohort])
        self.assertEqual(names, set().union(*cohort_names.values()))
        self.assertTrue(all(strategy_role(name) in {"benchmark", "baseline_candidate", "hybrid_control"}
                            for name in cohort_names["baseline"]))
        self.assertTrue(all(strategy_role(name) in {"benchmark", "baseline_candidate", "defensive_override"}
                            for name in cohort_names["defensive"]))
        self.assertTrue(all(strategy_role(name) in {"benchmark", "baseline_candidate", "tactical_opportunity"}
                            for name in cohort_names["tactical"]))


if __name__ == "__main__":
    unittest.main()
