import json
import unittest
from pathlib import Path

from research.tier1_etf_replay import SECTOR_ETFS, Tier1Config, _targets


ROOT = Path(__file__).resolve().parents[1] / "research"


class DailyEventConceptGenerationTests(unittest.TestCase):
    def test_declaration_and_job_match_without_router_or_search(self):
        declaration = json.loads((ROOT / "daily-event-concepts-generation-006.json").read_text())
        job = json.loads((ROOT / "daily-event-concepts-generation-006-job.json").read_text())
        candidates = {row["strategy"] for row in declaration["candidates"]}
        self.assertEqual(6, len(candidates))
        self.assertTrue(candidates <= set(job["tier1_config"]["strategy_names"]))
        self.assertTrue(declaration["constraints"]["no_parameter_search"])
        self.assertTrue(declaration["constraints"]["no_strategy_combination"])
        self.assertTrue(declaration["constraints"]["no_router_training"])
        self.assertEqual("2026-09-03", declaration["retrospective_evidence_end"])

    def test_new_candidates_are_long_only_and_normalized(self):
        config = Tier1Config(universe_name="ETF_GENERAL_CONCEPTS")
        symbols = ("SPY", "SHY", "BIL", "IEF", "GLD") + SECTOR_ETFS
        histories = {
            symbol: [100 + index * (.01 + offset * .001) for index in range(300)]
            for offset, symbol in enumerate(symbols)
        }
        for strategy in (
            "VOLATILITY_CONTRACTION_BREAKOUT", "DONCHIAN_TREND_BREAKOUT",
            "SECTOR_MOMENTUM_ACCELERATION", "BREADTH_THRUST_RECOVERY",
            "OVERSOLD_TREND_REBOUND", "BREADTH_DETERIORATION_DEFENSIVE",
        ):
            target = _targets(strategy, histories, config)
            self.assertAlmostEqual(1.0, sum(target.values()))
            self.assertTrue(all(weight >= 0 for weight in target.values()))

    def test_donchian_requires_trend_and_uses_recent_breakout_persistence(self):
        config = Tier1Config(universe_name="ETF_GENERAL_CONCEPTS")
        histories = {symbol: [100.0] * 210 for symbol in ("SPY", "SHY") + SECTOR_ETFS}
        histories["XLK"] = [100 + index * .1 for index in range(210)]
        self.assertEqual({"XLK": 1.0}, _targets("DONCHIAN_TREND_BREAKOUT", histories, config))
        histories["XLK"] = [200 - index * .1 for index in range(210)]
        self.assertEqual({"SHY": 1.0}, _targets("DONCHIAN_TREND_BREAKOUT", histories, config))

    def test_oversold_rebound_requires_reversal_and_long_trend(self):
        config = Tier1Config(universe_name="ETF_GENERAL_CONCEPTS")
        histories = {symbol: [100.0] * 220 for symbol in ("SPY", "SHY") + SECTOR_ETFS}
        spy = [100 + index * .1 for index in range(220)]
        spy[-5:] = [120, 116, 113, 112, 113]
        histories["SPY"] = spy
        self.assertEqual({"SPY": 1.0}, _targets("OVERSOLD_TREND_REBOUND", histories, config))
        histories["SPY"][-1] = 111
        self.assertEqual({"SHY": 1.0}, _targets("OVERSOLD_TREND_REBOUND", histories, config))


if __name__ == "__main__":
    unittest.main()
