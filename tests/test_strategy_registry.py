import unittest

from research.strategy_registry import (
    HYPOTHESES, registry_snapshot, validate_experiment_declaration,
)


class StrategyRegistryTests(unittest.TestCase):
    def test_registry_includes_all_research_waves(self):
        self.assertIn("VOL_MANAGED_SPY", HYPOTHESES)
        self.assertIn("SECTOR_ETF_ROTATION", HYPOTHESES)
        self.assertIn("ML_META_ALLOCATOR", HYPOTHESES)

    def test_snapshot_is_stable_except_capture_time(self):
        first = registry_snapshot(); second = registry_snapshot()
        self.assertEqual(first["sha256"], second["sha256"])

    def test_declaration_normalizes_known_hypothesis(self):
        result = validate_experiment_declaration({
            "hypothesis_id": "vol_managed_spy", "trial_id": "trial-001",
        })
        self.assertEqual("VOL_MANAGED_SPY", result["hypothesis_id"])

    def test_declaration_rejects_unknown_hypothesis(self):
        with self.assertRaisesRegex(ValueError, "unknown hypothesis_id"):
            validate_experiment_declaration({"hypothesis_id": "magic_alpha"})

    def test_legacy_job_without_declaration_remains_valid(self):
        self.assertEqual({}, validate_experiment_declaration(None))


if __name__ == "__main__":
    unittest.main()
