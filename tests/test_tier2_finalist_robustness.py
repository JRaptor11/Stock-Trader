import unittest

from research.tier2_finalist_robustness import FINALISTS


class Tier2FinalistRobustnessTests(unittest.TestCase):
    def test_finalist_roster_is_frozen_and_excludes_failed_router(self):
        self.assertEqual(4, len(FINALISTS))
        self.assertIn("STATIC_MULTI_SLEEVE", FINALISTS)
        self.assertNotIn("REGIME_MULTI_SLEEVE", FINALISTS)


if __name__ == "__main__":
    unittest.main()
