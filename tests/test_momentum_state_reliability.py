import csv
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from research.momentum_state_reliability import build


class MomentumStateReliabilityTests(unittest.TestCase):
    def _archive(self, path: Path, start_year: int) -> None:
        dates = [f"{start_year + offset // 8}-{1 + (offset % 8):02d}-03" for offset in range(16)]
        daily = []
        for strategy, multiplier in (("SPY_BUY_HOLD", 1.001), ("CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE", 1.002), ("DIVERSIFIED_TREND", 1.0015)):
            for cost in (10.0, 20.0):
                equity = 100000.0
                for date in dates:
                    equity *= multiplier
                    daily.append({"date": date, "strategy": strategy, "cost_bps": cost, "equity": equity})
        labels = [{"date": date, "trend_state": "BULL_DECELERATING" if index % 2 == 0 else "BEAR_RECOVERING",
                   "core_state": "BULL_ACCELERATING__HIGH_VOL__BROAD_BREADTH" if index % 3 == 0 else "OTHER"}
                  for index, date in enumerate(dates)]
        with zipfile.ZipFile(path, "w") as bundle:
            for name, rows in (("tier1_daily.csv", daily), ("tier1_market_state_labels.csv", labels)):
                stream = io.StringIO(); writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
                bundle.writestr(name, stream.getvalue())

    def test_outputs_payoff_and_gate_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); older = root / "older.zip"; modern = root / "modern.zip"
            self._archive(older, 2008); self._archive(modern, 2012)
            summary = build(older, modern, root / "out")
            self.assertEqual(3, summary["hypotheses"])
            header = (root / "out" / "reliability_aggregate_scorecard.csv").read_text().splitlines()[0]
            self.assertIn("profit_factor", header)
            self.assertIn("maximum_consecutive_losses", header)
            self.assertEqual("none", summary["router_effect"])


if __name__ == "__main__":
    unittest.main()
