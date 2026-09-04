import unittest
from research.statistical_safeguards import return_evidence

class StatisticalSafeguardTests(unittest.TestCase):
    def test_family_adjustment_and_winner_removal_are_reported(self):
        result=return_evidence([.5,.01,-.01,.02],10)
        self.assertGreaterEqual(result["bonferroni_adjusted_p"],result["approximate_two_sided_p"])
        self.assertLess(result["return_without_best_trade"],.5)

if __name__=="__main__": unittest.main()
