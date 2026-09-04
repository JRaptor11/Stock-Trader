import unittest

from research.historical_replay import ReplayConfig, _universe_selection_diagnostics
from research.universes import UNIVERSES, resolve_universe, universe_metadata


class ResearchUniverseTests(unittest.TestCase):
    def test_named_universes_are_nested_and_unique(self):
        baseline = set(UNIVERSES["BASELINE_10"])
        twenty = set(UNIVERSES["DIVERSIFIED_20"])
        thirty = set(UNIVERSES["DIVERSIFIED_30"])
        self.assertEqual(10, len(baseline))
        self.assertEqual(20, len(twenty))
        self.assertEqual(30, len(thirty))
        self.assertTrue(baseline < twenty < thirty)

    def test_config_rejects_named_and_custom_universe_together(self):
        with self.assertRaisesRegex(ValueError, "universe_name or candidate_symbols"):
            ReplayConfig(universe_name="BASELINE_10", candidate_symbols=("AAPL",))

    def test_universe_metadata_reports_sector_concentration(self):
        metadata = universe_metadata("BASELINE_10", resolve_universe("BASELINE_10"))
        self.assertEqual(10, metadata["symbol_count"])
        self.assertGreater(metadata["sector_counts"]["technology"], 1)
        self.assertGreater(metadata["largest_sector_pct"], 0)
        self.assertFalse(metadata["survivorship_bias_controlled"])

    def test_larger_universe_requires_proportional_fresh_coverage(self):
        config = ReplayConfig(universe_name="DIVERSIFIED_30")
        self.assertEqual(80.0, config.minimum_eligible_coverage_pct)

    def test_tier1_etf_universe_is_stable_and_classified(self):
        symbols = resolve_universe("ETF_TIER1_RESEARCH")
        self.assertEqual(15, len(symbols))
        self.assertEqual(len(symbols), len(set(symbols)))
        metadata = universe_metadata("ETF_TIER1_RESEARCH", symbols)
        self.assertFalse(metadata["survivorship_bias_controlled"])
        self.assertTrue(metadata["constituent_survivorship_avoided"])
        self.assertNotIn("unclassified", metadata["sector_counts"])

    def test_generation_3_adds_predeclared_cross_asset_etfs(self):
        original = set(resolve_universe("ETF_TIER1_RESEARCH"))
        expanded = set(resolve_universe("ETF_GENERATION_3"))
        self.assertTrue(original < expanded)
        self.assertEqual({"IEF", "TLT", "GLD", "DBC", "EFA", "EEM", "VNQ"}, expanded - original)
        metadata = universe_metadata("ETF_GENERATION_3", tuple(sorted(expanded)))
        self.assertNotIn("unclassified", metadata["sector_counts"])

    def test_selection_diagnostics_group_by_strategy_symbol_and_sector(self):
        rows = _universe_selection_diagnostics([
            {"strategy_name": "S", "symbol": "AAPL", "selected": True, "raw_target_weight": 0.2},
            {"strategy_name": "S", "symbol": "AAPL", "selected": False, "raw_target_weight": 0.0},
        ])
        self.assertEqual(1, len(rows))
        self.assertEqual("technology", rows[0]["sector"])
        self.assertEqual(0.5, rows[0]["selection_rate"])


if __name__ == "__main__":
    unittest.main()
