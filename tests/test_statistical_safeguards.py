import json
import math
import unittest
from research.statistical_safeguards import (
    combinatorially_symmetric_cross_validation,
    deflated_sharpe_ratio,
    multiple_testing_report,
    paired_block_bootstrap,
    return_evidence,
    white_reality_check,
)

class StatisticalSafeguardTests(unittest.TestCase):
    def test_paired_block_bootstrap_is_deterministic_and_paired(self):
        result=paired_block_bootstrap([.02,.01,.03,.02],[0.,0.,0.,0.],block_size=2,samples=100)
        self.assertGreater(result["excess_return_ci_95"][0],0)
        self.assertEqual(result,paired_block_bootstrap([.02,.01,.03,.02],[0.,0.,0.,0.],block_size=2,samples=100))
        with self.assertRaisesRegex(ValueError,"paired"):
            paired_block_bootstrap([.01],[.01,.02])

    def test_family_adjustment_and_winner_removal_are_reported(self):
        result=return_evidence([.5,.01,-.01,.02],10)
        self.assertGreaterEqual(result["bonferroni_adjusted_p"],result["approximate_two_sided_p"])
        self.assertLess(result["return_without_best_trade"],.5)

    def test_deflated_sharpe_reports_family_threshold_and_nonnormality(self):
        returns = {
            "winner": [.012, .010, .011, .009] * 60,
            "flat": [.001, -.001, .001, -.001] * 60,
            "loser": [-.004, .003, -.004, .003] * 60,
        }
        result = deflated_sharpe_ratio(
            returns, selected_strategy="winner", family_trials=3,
        )
        self.assertEqual("winner", result["selected_strategy"])
        self.assertGreater(result["expected_max_periodic_sharpe_under_null"], 0)
        self.assertGreater(result["deflated_sharpe_probability"], .95)
        self.assertTrue(result["passes_95_percent_probability"])
        self.assertTrue(result["trial_sharpe_dispersion_estimated"])
        self.assertEqual([], result["zero_variance_trials_excluded_from_dispersion"])

    def test_deflated_sharpe_discloses_zero_variance_family_members(self):
        result = deflated_sharpe_ratio({
            "winner": [.01, .02, .01, .02] * 20,
            "cash": [0.] * 80,
        }, selected_strategy="winner")
        self.assertEqual(["cash"], result["zero_variance_trials_excluded_from_dispersion"])
        self.assertFalse(result["trial_sharpe_dispersion_estimated"])

    def test_deflated_sharpe_rejects_unaligned_or_degenerate_selected_series(self):
        with self.assertRaisesRegex(ValueError, "aligned"):
            deflated_sharpe_ratio({"a": [.1, .2, .3], "b": [.1, .2]})
        with self.assertRaisesRegex(ValueError, "zero return variance"):
            deflated_sharpe_ratio({"a": [.1, .1, .1]}, selected_strategy="a")

    def test_cscv_detects_deliberately_reversing_selection(self):
        returns = {
            "a": [.02] * 4 + [.02] * 4 + [-.02] * 4 + [-.02] * 4,
            "b": [-.02] * 4 + [-.02] * 4 + [.02] * 4 + [.02] * 4,
        }
        result = combinatorially_symmetric_cross_validation(returns, partitions=4)
        self.assertEqual(6, result["split_count"])
        self.assertEqual(1.0, result["probability_of_backtest_overfitting"])
        self.assertTrue(all(row["classified_overfit"] for row in result["split_results"]))
        self.assertEqual(result, combinatorially_symmetric_cross_validation(returns, partitions=4))

    def test_cscv_constant_fold_scores_remain_strict_json(self):
        returns = {
            "positive_then_negative": [.02] * 8 + [-.02] * 8,
            "negative_then_positive": [-.02] * 8 + [.02] * 8,
        }
        result = combinatorially_symmetric_cross_validation(returns, partitions=4)
        encoded = json.dumps(result, allow_nan=False)
        self.assertIn("positive_infinity", encoded)
        self.assertTrue(all(math.isfinite(row["logit"]) for row in result["split_results"]))

    def test_cscv_bounds_combinatorial_work(self):
        returns = {"a": [.01] * 20, "b": [0.] * 20}
        with self.assertRaisesRegex(ValueError, "above max_splits"):
            combinatorially_symmetric_cross_validation(
                returns, partitions=20, max_splits=100,
            )

    def test_white_reality_check_is_deterministic_and_detects_large_edge(self):
        benchmark = [0.] * 240
        strategies = {
            "strong": [.011, .009, .010, .012] * 60,
            "noise": [.001, -.001, .001, -.001] * 60,
        }
        result = white_reality_check(
            strategies, benchmark, bootstrap_samples=299, mean_block_length=5, seed=3,
        )
        self.assertEqual("strong", result["best_strategy"])
        self.assertLessEqual(result["p_value"], .05)
        self.assertTrue(result["rejects_equal_or_inferior_family_at_05"])
        self.assertEqual(
            result,
            white_reality_check(
                strategies, benchmark, bootstrap_samples=299,
                mean_block_length=5, seed=3,
            ),
        )

    def test_white_reality_check_does_not_reject_zero_edge(self):
        benchmark = [.01, -.01] * 30
        result = white_reality_check(
            {"same": list(benchmark)}, benchmark, bootstrap_samples=99,
        )
        self.assertEqual(1.0, result["p_value"])
        self.assertFalse(result["rejects_equal_or_inferior_family_at_05"])

    def test_combined_report_is_serializable_shape(self):
        benchmark = [0.] * 40
        strategies = {
            "a": [.01, .02, -.01, .01] * 10,
            "b": [.005, -.005, .005, -.005] * 10,
        }
        report = multiple_testing_report(
            strategies, benchmark, selected_strategy="a", family_trials=4,
            cscv_partitions=4, bootstrap_samples=25,
        )
        self.assertEqual(1, report["schema_version"])
        self.assertEqual(
            {"schema_version", "diagnostic_only", "deflated_sharpe", "cscv_pbo",
             "white_reality_check"},
            set(report),
        )
        json.dumps(report, allow_nan=False)

if __name__=="__main__": unittest.main()
