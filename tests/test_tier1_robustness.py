import unittest

from research.tier1_robustness import (
    annual_comparison, block_bootstrap, drawdown_profile, rolling_comparison,
)


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

    def test_annual_comparison_compounds_each_calendar_year(self):
        candidate = [
            {"date": "2025-12-31", "return": .10, "equity": 110},
            {"date": "2026-01-02", "return": .05, "equity": 115.5},
        ]
        benchmark = [
            {"date": "2025-12-31", "return": .05, "equity": 105},
            {"date": "2026-01-02", "return": .01, "equity": 106.05},
        ]
        rows = annual_comparison(candidate, benchmark, "2025-01-01")
        self.assertEqual(["2025", "2026"], [row["year"] for row in rows])
        self.assertTrue(all(row["candidate_won"] for row in rows))

    def test_drawdown_profile_reports_recovery(self):
        rows = [
            {"date": "2026-01-01", "equity": 100.0},
            {"date": "2026-01-02", "equity": 80.0},
            {"date": "2026-01-03", "equity": 90.0},
            {"date": "2026-01-04", "equity": 101.0},
        ]
        result = drawdown_profile(rows)
        self.assertAlmostEqual(-.20, result["max_drawdown"])
        self.assertEqual("2026-01-04", result["recovery_date"])
        self.assertEqual(2, result["recovery_sessions"])


if __name__=="__main__": unittest.main()
