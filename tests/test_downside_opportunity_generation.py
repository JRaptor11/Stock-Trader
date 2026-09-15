import json
import unittest
from pathlib import Path

from research.tier1_etf_replay import DEFENSIVE_ETFS, SECTOR_ETFS, Tier1Config, _targets


ROOT = Path(__file__).resolve().parents[1] / "research"
CANDIDATES = (
    "EQUITY_TREND_DEFENSIVE", "DRAWDOWN_BRAKE", "VOLATILITY_SHOCK_DEFENSIVE",
    "TREND_PULLBACK_REBOUND", "FAILED_BREAKDOWN_RECOVERY", "DEFENSIVE_ASSET_BREAKOUT",
)


class DownsideOpportunityGenerationTests(unittest.TestCase):
    def _histories(self, length=260):
        symbols = ("SPY", "SHY") + DEFENSIVE_ETFS + SECTOR_ETFS
        return {symbol: [100 + index * .05 for index in range(length)] for symbol in symbols}

    def test_declaration_and_job_are_frozen_and_isolated(self):
        declaration = json.loads((ROOT / "downside-opportunity-generation-007.json").read_text())
        job = json.loads((ROOT / "downside-opportunity-generation-007-job.json").read_text())
        self.assertEqual(set(CANDIDATES), {row["strategy"] for row in declaration["candidates"]})
        self.assertTrue(set(CANDIDATES) <= set(job["tier1_config"]["strategy_names"]))
        self.assertTrue(all(declaration["constraints"].values()))
        self.assertEqual("2026-09-03", declaration["retrospective_evidence_end"])

    def test_all_candidates_are_long_only_and_normalized(self):
        histories = self._histories()
        config = Tier1Config(universe_name="ETF_GENERAL_CONCEPTS")
        for strategy in CANDIDATES:
            targets = _targets(strategy, histories, config)
            self.assertAlmostEqual(1.0, sum(targets.values()))
            self.assertTrue(all(weight >= 0 for weight in targets.values()))

    def test_drawdown_brake_enters_and_leaves_cash(self):
        histories = self._histories(100)
        config = Tier1Config(universe_name="ETF_GENERAL_CONCEPTS")
        histories["SPY"][-20:] = [110 - index for index in range(20)]
        self.assertEqual({"SHY": 1.0}, _targets("DRAWDOWN_BRAKE", histories, config))
        histories["SPY"] = [100 + index * .1 for index in range(100)]
        self.assertEqual({"SPY": 1.0}, _targets("DRAWDOWN_BRAKE", histories, config))

    def test_trend_pullback_requires_reversal_inside_uptrend(self):
        histories = self._histories()
        config = Tier1Config(universe_name="ETF_GENERAL_CONCEPTS")
        histories["XLK"][-4:] = [120, 112, 111, 114]
        self.assertEqual({"XLK": 1.0}, _targets("TREND_PULLBACK_REBOUND", histories, config))
        histories["XLK"][-1] = 108
        self.assertEqual({"SHY": 1.0}, _targets("TREND_PULLBACK_REBOUND", histories, config))

    def test_failed_breakdown_requires_reclaim_and_long_trend(self):
        histories = self._histories()
        config = Tier1Config(universe_name="ETF_GENERAL_CONCEPTS")
        floor = min(histories["SPY"][-25:-5])
        histories["SPY"][-4:] = [floor - 1, floor - .5, floor + .2, floor + 1]
        self.assertEqual({"SPY": 1.0}, _targets("FAILED_BREAKDOWN_RECOVERY", histories, config))
        histories["SPY"][-1] = floor - 2
        self.assertEqual({"SHY": 1.0}, _targets("FAILED_BREAKDOWN_RECOVERY", histories, config))


if __name__ == "__main__":
    unittest.main()
