import unittest

from research.portfolio_insurance_validation import simulate_tipp


class PortfolioInsuranceValidationTests(unittest.TestCase):
    def test_tipp_reduces_exposure_after_loss_without_leverage(self):
        path = simulate_tipp([
            ("2020-01-02", 0.10),
            ("2020-01-03", -0.15),
            ("2020-01-06", -0.10),
        ], 10.0)
        self.assertLess(path[-1]["equity_weight"], path[0]["equity_weight"])
        self.assertTrue(all(0.0 <= row["equity_weight"] <= 1.0 for row in path))

    def test_no_trade_band_suppresses_small_rebalance(self):
        path = simulate_tipp([("2020-01-02", 0.001)], 10.0)
        self.assertEqual(0.0, path[0]["turnover"])
        self.assertEqual(1.0, path[0]["equity_weight"])


if __name__ == "__main__":
    unittest.main()
