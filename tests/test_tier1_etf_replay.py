import csv
import json
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

from research.tier1_etf_replay import (
    STRATEGIES, Tier1Config, config_from_job, load_daily_bars, run_tier1_job,
)
from research.universes import resolve_universe


def write_bars(path: Path, sessions: int = 280):
    symbols = resolve_universe("ETF_TIER1_RESEARCH")
    day = date(2024, 1, 2); rows = []
    for index in range(sessions):
        while day.weekday() >= 5: day += timedelta(days=1)
        for offset, symbol in enumerate(symbols):
            # Differentiated, deterministic trends make rankings testable.
            close = 100 + index * (0.02 + offset * 0.002)
            rows.append({"timestamp": day.isoformat()+"T21:00:00Z", "symbol": symbol,
                         "open": close-0.05, "high": close+0.1, "low": close-0.1,
                         "close": close, "volume": 1000000, "trade_count": 1000, "vwap": close})
        day += timedelta(days=1)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer=csv.DictWriter(handle,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)


class Tier1ETFReplayTests(unittest.TestCase):
    def test_config_rejects_unknown_fields(self):
        with self.assertRaisesRegex(ValueError, "unknown tier1_config"):
            config_from_job({"tier1_config": {"future_leak": True}})

    def test_config_requires_complete_nonoverlapping_holdout(self):
        with self.assertRaisesRegex(ValueError, "must be set together"):
            Tier1Config(discovery_end_date="2024-09-02")
        with self.assertRaisesRegex(ValueError, "must precede"):
            Tier1Config(discovery_end_date="2024-09-03", holdout_start_date="2024-09-03")

    def test_daily_loader_aggregates_intraday_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"bars.csv"
            path.write_text("timestamp,symbol,open,high,low,close\n2026-01-02T14:30:00Z,SPY,100,102,99,101\n2026-01-02T20:55:00Z,SPY,101,103,100,102\n",encoding="utf-8")
            dates,bars=load_daily_bars(path,{"SPY"})
            self.assertEqual(["2026-01-02"],dates); self.assertEqual(100,bars[dates[0]]["SPY"]["open"]); self.assertEqual(102,bars[dates[0]]["SPY"]["close"])

    def test_tournament_writes_all_costs_strategies_and_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); bars=root/"bars.csv"; archive=root/"result.zip"; write_bars(bars)
            job={"engine":"tier1_etf_daily","experiment":{"hypothesis_id":"ETF_DUAL_MOMENTUM","trial_id":"trial-001"},"tier1_config":{"cost_ladder_bps":[1,10],"primary_cost_bps":10,"discovery_end_date":"2024-09-02","holdout_start_date":"2024-09-03"}}
            run_tier1_job(job,bars,archive,"abc123")
            with zipfile.ZipFile(archive) as bundle:
                names=set(bundle.namelist()); manifest=json.loads(bundle.read("tier1_manifest.json")); summary=json.loads(bundle.read("tier1_summary.json"))
                self.assertIn("tier1_cost_ladder_scorecard.csv",names); self.assertIn("tier1_promotion_gates.csv",names)
                self.assertIn("tier1_period_scorecard.csv",names)
                self.assertEqual("signal_at_close_fill_at_next_available_open",manifest["execution_semantics"])
                self.assertEqual(set(STRATEGIES),{row["strategy"] for row in summary["scorecards"]})
                self.assertEqual("holdout",summary["promotion_period"])
                self.assertTrue(all(not row["paper_trading_approved"] for row in summary["promotion_gates"]))


if __name__ == "__main__": unittest.main()
