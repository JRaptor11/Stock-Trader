import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from research.bear_recovering_specialist_validation import build_validation


STRATEGIES = (
    "SPY_BUY_HOLD",
    "CROSS_ASSET_DUAL_MOMENTUM",
    "CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE",
    "DIVERSIFIED_TREND",
)


def _archive(path: Path, year: int) -> None:
    dates = [f"{year}-01-{day:02d}" for day in range(1, 25)]
    daily = []
    equity = {strategy: 100000.0 for strategy in STRATEGIES}
    for index, day in enumerate(dates):
        for strategy in STRATEGIES:
            value = 0.001 if strategy == "SPY_BUY_HOLD" else 0.002
            equity[strategy] *= 1.0 + value
            for cost in (10.0, 20.0):
                daily.append({"date": day, "strategy": strategy, "cost_bps": cost,
                              "equity": equity[strategy]})
    labels = [{"date": day, "trend_state":
               "BEAR_RECOVERING" if index % 4 in (0, 1) else "BULL_ACCELERATING"}
              for index, day in enumerate(dates)]
    with zipfile.ZipFile(path, "w") as bundle:
        for name, rows in (("tier1_daily.csv", daily),
                           ("tier1_market_state_labels.csv", labels)):
            header = list(rows[0])
            text = [",".join(header)]
            text.extend(",".join(str(row[key]) for key in header) for row in rows)
            bundle.writestr(name, "\n".join(text) + "\n")


class BearRecoveringSpecialistValidationTests(unittest.TestCase):
    def test_builds_causal_episode_outputs_without_router_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = root / "older.zip"
            modern = root / "modern.zip"
            _archive(older, 2010)
            _archive(modern, 2012)
            summary = build_validation(older, modern, root / "output")
            self.assertEqual("none", summary["router_effect"])
            self.assertEqual("none", summary["generation_015_effect"])
            self.assertTrue((root / "output" / "phase012_episode_scorecard.csv").is_file())
            self.assertTrue((root / "output" / "phase012_acceptance.csv").is_file())
            saved = json.loads((root / "output" / "phase012_summary.json").read_text())
            self.assertEqual("BEAR_RECOVERING", saved["state"])


if __name__ == "__main__":
    unittest.main()
