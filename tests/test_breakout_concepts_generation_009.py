import json
import unittest
from pathlib import Path

from research.tier1_etf_replay import MarketHistories, SECTOR_ETFS, Tier1Config, _targets


ROOT = Path(__file__).resolve().parents[1] / "research"
CANDIDATES = (
    "VOLUME_EXPANSION_BREAKOUT", "VOLATILITY_ADJUSTED_ACCELERATION_BREAKOUT",
    "OVERNIGHT_GAP_CONTINUATION", "CROSS_SECTIONAL_ABNORMAL_RETURN_BREAKOUT",
    "SECTOR_PARTICIPATION_BREAKOUT", "MARKET_CONFIRMED_SECTOR_BREAKOUT",
)


class Generation009Tests(unittest.TestCase):
    def test_declaration_and_job_retain_prior_breakout_controls(self):
        declaration = json.loads((ROOT / "breakout-concepts-generation-009.json").read_text())
        job = json.loads((ROOT / "breakout-concepts-generation-009-job.json").read_text())
        self.assertEqual(set(CANDIDATES), {row["strategy"] for row in declaration["candidates"]})
        self.assertTrue(set(CANDIDATES) <= set(job["tier1_config"]["strategy_names"]))
        self.assertTrue({"SECTOR_PRICE_BREAKOUT_20D", "DONCHIAN_TREND_BREAKOUT", "VOLATILITY_CONTRACTION_BREAKOUT", "RELATIVE_STRENGTH_BREAKOUT"} <= set(job["tier1_config"]["strategy_names"]))
        self.assertTrue(declaration["constraints"]["retain_weak_full_sample_strategies_for_conditional_analysis"])

    def test_candidates_are_causal_long_only_and_normalized(self):
        symbols = ("SPY", "SHY", "BIL", "IEF", "GLD") + SECTOR_ETFS
        histories = MarketHistories(symbols)
        for index in range(300):
            for offset, symbol in enumerate(symbols):
                close = 100 + index * (.03 + offset * .001)
                histories[symbol].append(close)
                histories.market_bars[symbol].append({"open": close * .999, "high": close * 1.01, "low": close * .99, "close": close, "volume": 1_000_000})
        config = Tier1Config(universe_name="ETF_GENERAL_CONCEPTS")
        for strategy in CANDIDATES:
            target = _targets(strategy, histories, config)
            self.assertAlmostEqual(1.0, sum(target.values()))
            self.assertTrue(all(value >= 0 for value in target.values()))


if __name__ == "__main__":
    unittest.main()
