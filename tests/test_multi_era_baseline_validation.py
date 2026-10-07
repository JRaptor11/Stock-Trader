import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from research.multi_era_baseline_validation import ERAS, build_validation


def _archive(path: Path, dates: list[str], challenger_multiplier: float) -> None:
    daily = []
    equity = {"SPY_BUY_HOLD": 100000.0, "STATIC_60_30_10": 100000.0}
    for index, day in enumerate(dates):
        for strategy in equity:
            daily_return = 0.001 if strategy == "SPY_BUY_HOLD" else 0.001 * challenger_multiplier
            equity[strategy] *= 1 + daily_return
            for cost in (10.0, 20.0):
                daily.append({"date": day, "strategy": strategy, "cost_bps": cost,
                              "equity": equity[strategy], "cash": 0, "positions": 1})
    labels = [{"date": day, "trend_state": "BULL_DECELERATING"} for day in dates]
    with zipfile.ZipFile(path, "w") as bundle:
        for name, rows in (("tier1_daily.csv", daily),
                           ("tier1_market_state_labels.csv", labels)):
            text = []
            header = list(rows[0])
            text.append(",".join(header))
            text.extend(",".join(str(row[key]) for key in header) for row in rows)
            bundle.writestr(name, "\n".join(text) + "\n")


class MultiEraBaselineValidationTests(unittest.TestCase):
    def test_fixed_eras_are_non_overlapping_and_exclude_2011(self):
        previous_end = ""
        for _, _, start, end, _ in ERAS:
            self.assertGreater(start, previous_end)
            previous_end = end
        self.assertFalse(any(start <= "2011-06-01" <= end for _, _, start, end, _ in ERAS))

    def test_builds_bounded_secondary_outputs_without_resimulation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older_dates = [f"2010-01-{day:02d}" for day in range(1, 29)]
            modern_dates = ([f"2012-01-{day:02d}" for day in range(1, 29)] +
                            [f"2015-01-{day:02d}" for day in range(1, 29)] +
                            [f"2018-01-{day:02d}" for day in range(1, 29)] +
                            [f"2021-01-{day:02d}" for day in range(1, 29)] +
                            [f"2024-01-{day:02d}" for day in range(1, 29)])
            prehistory = root / "older.zip"; modern = root / "modern.zip"
            _archive(prehistory, older_dates, 1.2); _archive(modern, modern_dates, 1.2)
            summary = build_validation(prehistory, modern, root / "output")
            self.assertEqual(6, summary["eras"])
            self.assertEqual(1, summary["strategies"])
            self.assertEqual("none", summary["router_effect"])
            self.assertTrue((root / "output" / "phase011_era_scorecard.csv").is_file())
            self.assertTrue((root / "output" / "phase011_state_episode_scorecard.csv").is_file())
            saved = json.loads((root / "output" / "phase011_summary.json").read_text())
            self.assertEqual("none", saved["paper_trading_effect"])


if __name__ == "__main__":
    unittest.main()
