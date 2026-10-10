import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from research.bear_recovery_state_timing import build_validation


def _archive(path: Path, year: int) -> None:
    dates = [f"{year}-01-{day:02d}" for day in range(1, 29)]
    daily = []
    equity = {"SPY_BUY_HOLD": 100000.0, "DIVERSIFIED_TREND": 100000.0}
    for day in dates:
        for strategy in equity:
            equity[strategy] *= 1.001 if strategy == "SPY_BUY_HOLD" else 1.002
            for cost in (10.0, 20.0):
                daily.append({"date": day, "strategy": strategy,
                              "cost_bps": cost, "equity": equity[strategy]})
    labels = []
    for index, day in enumerate(dates):
        phase = index % 7
        labels.append({
            "date": day,
            "raw_trend_state": "BEAR_RECOVERING" if phase in (0, 1, 2) else "BULL_ACCELERATING",
            "trend_state": "BEAR_RECOVERING" if phase in (2, 3) else "BULL_ACCELERATING",
        })
    with zipfile.ZipFile(path, "w") as bundle:
        for name, rows in (("tier1_daily.csv", daily),
                           ("tier1_market_state_labels.csv", labels)):
            header = list(rows[0])
            text = [",".join(header)]
            text.extend(",".join(str(row[key]) for key in header) for row in rows)
            bundle.writestr(name, "\n".join(text) + "\n")


class BearRecoveryStateTimingTests(unittest.TestCase):
    def test_builds_timing_and_false_start_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = root / "older.zip"
            modern = root / "modern.zip"
            _archive(older, 2010)
            _archive(modern, 2012)
            summary = build_validation(older, modern, root / "output")
            self.assertEqual("none", summary["router_effect"])
            self.assertEqual("none", summary["paper_trading_effect"])
            self.assertTrue((root / "output" / "phase013_event_scorecard.csv").is_file())
            self.assertTrue((root / "output" / "phase013_confirmation_delay.csv").is_file())
            saved = json.loads((root / "output" / "phase013_summary.json").read_text())
            self.assertEqual("DIVERSIFIED_TREND", saved["strategy"])


if __name__ == "__main__":
    unittest.main()
