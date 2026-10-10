import unittest

from research.role_aware_baseline_reassessment import _bool


class RoleAwareBaselineReassessmentTests(unittest.TestCase):
    def test_csv_boolean_parser_is_explicit(self):
        self.assertTrue(_bool("True"))
        self.assertFalse(_bool("False"))


if __name__ == "__main__":
    unittest.main()
