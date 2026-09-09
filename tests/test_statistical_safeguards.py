import unittest
from research.statistical_safeguards import paired_block_bootstrap, return_evidence

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

if __name__=="__main__": unittest.main()
