import unittest

from research.fully_invested_equity_validation import _is_quarterly_rebalance


class FullyInvestedEquityValidationTests(unittest.TestCase):
    def test_rebalances_only_on_first_observation_of_quarter_month(self):
        self.assertTrue(_is_quarterly_rebalance("2024-04-01", "2024-03-28"))
        self.assertFalse(_is_quarterly_rebalance("2024-04-02", "2024-04-01"))
        self.assertFalse(_is_quarterly_rebalance("2024-05-01", "2024-04-30"))


if __name__ == "__main__":
    unittest.main()
