import unittest

from research.tier1_robustness import block_bootstrap, rolling_comparison


class Tier1RobustnessTests(unittest.TestCase):
    def test_rolling_comparison_uses_only_holdout(self):
        candidate=[{"date":f"2025-01-{i:02d}","return":.01,"equity":100} for i in range(1,29)]
        benchmark=[{"date":f"2025-01-{i:02d}","return":0,"equity":100} for i in range(1,29)]
        rows=rolling_comparison(candidate,benchmark,"2025-01-10",windows=(5,),step=5)
        self.assertTrue(rows); self.assertGreaterEqual(rows[0]["start"],"2025-01-10"); self.assertTrue(all(r["candidate_won"] for r in rows))

    def test_paired_bootstrap_is_deterministic(self):
        candidate=[{"date":f"2025-01-{i:02d}","return":.001,"equity":100} for i in range(1,29)]
        benchmark=[{"date":f"2025-01-{i:02d}","return":0,"equity":100} for i in range(1,29)]
        first=block_bootstrap(candidate,benchmark,"2025-01-01",samples=100,block=5)
        second=block_bootstrap(candidate,benchmark,"2025-01-01",samples=100,block=5)
        self.assertEqual(first,second); self.assertEqual(1.0,first["probability_excess_positive"])


if __name__=="__main__": unittest.main()
