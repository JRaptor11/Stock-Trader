import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from research.strategy_evidence_catalog import _merge_conditions, load_archives, write_bundle


def _csv(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader(); writer.writerows(rows)
    return output.getvalue()


def _archive(path, daily_name, strategy, equities, include_conditions=False, cost=10):
    daily = []
    for index, equity in enumerate(equities, 1):
        daily.append({"date": f"2026-01-{index:02d}", "strategy": strategy, "cost_bps": cost, "equity": equity})
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(daily_name, _csv(daily))
        bundle.writestr("tier1_manifest.json", json.dumps({"experiment": {"hypothesis_id": "TIER1_ETF_TOURNAMENT", "trial_id": path.stem}}))
        if include_conditions:
            conditions = [{"date": f"2026-01-{index:02d}", "volatility_20d_bucket": "Q1_LOW" if index < 4 else "Q5_HIGH"} for index in range(1, len(equities) + 1)]
            bundle.writestr("tier1_market_conditions.csv", _csv(conditions))


class StrategyEvidenceCatalogTests(unittest.TestCase):
    def test_condition_merge_preserves_richer_diagnostics_in_either_order(self):
        sparse = {
            "date": "2026-01-02",
            "canonical_state_bucket": "BULL_LOW_VOL",
            "cross_asset_directional_magnitude_252d_bucket": "",
            "cross_asset_correlation_60d_bucket": "",
        }
        rich = {
            "date": "2026-01-02",
            "canonical_state_bucket": "BULL_LOW_VOL",
            "cross_asset_directional_magnitude_252d": "0.18",
            "cross_asset_directional_magnitude_252d_bucket": "Q5_HIGH",
            "cross_asset_correlation_60d": "0.31",
            "cross_asset_correlation_60d_bucket": "Q2_LOW",
        }
        for rows in ((sparse, rich), (rich, sparse)):
            with self.subTest(order=rows):
                conditions = {}
                for row in rows:
                    _merge_conditions(conditions, [row])
                merged = conditions["2026-01-02"]
                self.assertEqual("BULL_LOW_VOL", merged["canonical_state_bucket"])
                self.assertEqual("0.18", merged["cross_asset_directional_magnitude_252d"])
                self.assertEqual("Q5_HIGH", merged["cross_asset_directional_magnitude_252d_bucket"])
                self.assertEqual("0.31", merged["cross_asset_correlation_60d"])
                self.assertEqual("Q2_LOW", merged["cross_asset_correlation_60d_bucket"])

    def test_condition_merge_rejects_shared_nonblank_bucket_conflict(self):
        conditions = {}
        _merge_conditions(conditions, [{"date": "2026-01-02", "canonical_state_bucket": "BULL_LOW_VOL"}])
        with self.assertRaisesRegex(
            ValueError,
            "conflicting causal condition labels for 2026-01-02: canonical_state_bucket",
        ):
            _merge_conditions(
                conditions,
                [{"date": "2026-01-02", "canonical_state_bucket": "BEAR_HIGH_VOL"}],
            )

    def test_builds_matched_cross_family_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); first = root / "first.zip"; second = root / "second.zip"; output = root / "out.zip"
            _archive(first, "tier1_daily.csv", "SPY_BUY_HOLD", [100, 101, 102, 103, 104], True)
            _archive(second, "intraday_daily.csv", "OPENING_RANGE_BREAKOUT", [100, 102, 104, 103, 106])
            manifest = write_bundle([first, second], output, minimum_samples=2)
            self.assertEqual(4, manifest["common_return_sessions"])
            with zipfile.ZipFile(output) as bundle:
                rows = list(csv.DictReader(io.StringIO(bundle.read("strategy_condition_matrix.csv").decode())))
                self.assertTrue(rows)
                self.assertEqual({"SPY_BUY_HOLD", "OPENING_RANGE_BREAKOUT"}, {row["strategy"] for row in rows})
                self.assertTrue(all(row["comparison_start"] >= "2026-01-02" for row in rows))
                self.assertTrue(all(row["annualized_volatility"] for row in rows))

    def test_infers_tier2_and_records_bad_legacy_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tier2.zip"
            daily = [{"date": f"2026-01-{index:02d}", "strategy": strategy, "cost_bps": 10, "equity": 100 + index}
                     for strategy in ("SPY_BUY_HOLD", "FACTOR_ETF_MOMENTUM") for index in range(1, 4)]
            conditions = [{"date": f"2026-01-{index:02d}", "volatility_20d_bucket": "Q1_LOW"} for index in range(1, 4)]
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("tier1_daily.csv", _csv(daily))
                bundle.writestr("tier1_market_conditions.csv", _csv(conditions))
                bundle.writestr("tier1_manifest.json", json.dumps({"experiment": {"hypothesis_id": "TIER1_ETF_TOURNAMENT"}, "config": {"universe_name": "ETF_TIER2_MULTI_SLEEVE"}}))
            _observations, _conditions, sources = load_archives([path], 10)
            self.assertEqual("TIER2_ETF_TOURNAMENT", sources[0]["hypothesis_id"])
            self.assertIn("declared TIER1_ETF_TOURNAMENT", sources[0]["provenance_warning"])

    def test_fails_closed_without_conditions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "only.zip"
            _archive(path, "tier1_daily.csv", "SPY_BUY_HOLD", [100, 101])
            with self.assertRaisesRegex(ValueError, "causal market conditions"):
                load_archives([path], 10)

    def test_fails_closed_without_benchmark(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "only.zip"; output = Path(directory) / "out.zip"
            _archive(path, "tier1_daily.csv", "SECTOR_ETF_ROTATION", [100, 101, 102], True)
            with self.assertRaisesRegex(ValueError, "SPY_BUY_HOLD"):
                write_bundle([path], output, minimum_samples=1)

    def test_rejects_mismatched_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "only.zip"
            _archive(path, "tier1_daily.csv", "SPY_BUY_HOLD", [100, 101], True, cost=5)
            with self.assertRaisesRegex(ValueError, "no daily rows at 10"):
                load_archives([path], 10)

    def test_rejects_conflicting_benchmarks(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.zip"; second = Path(directory) / "second.zip"; output = Path(directory) / "out.zip"
            _archive(first, "tier1_daily.csv", "SPY_BUY_HOLD", [100, 101, 102], True)
            _archive(second, "intraday_daily.csv", "SPY_BUY_HOLD", [100, 102, 104])
            with self.assertRaisesRegex(ValueError, "returns conflict"):
                write_bundle([first, second], output, minimum_samples=1)

    def test_accepts_separate_condition_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); result = root / "result.zip"; condition_source = root / "conditions.zip"; output = root / "out.zip"
            _archive(result, "tier1_daily.csv", "SPY_BUY_HOLD", [100, 101, 102])
            _archive(condition_source, "intraday_daily.csv", "IGNORED", [100, 100, 100], True)
            manifest = write_bundle([result], output, minimum_samples=1, condition_archives=[condition_source])
            self.assertEqual(2, manifest["condition_labeled_sessions"])


if __name__ == "__main__":
    unittest.main()
