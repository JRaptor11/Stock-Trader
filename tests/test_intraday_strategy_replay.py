import csv, hashlib, json, tempfile, unittest, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from research.intraday_strategy_replay import IntradayBar, IntradayConfig, _parameter_stability, load_sessions, run_tournament


class IntradayStrategyReplayTests(unittest.TestCase):
    def test_regular_session_filter_handles_standard_and_daylight_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"bars.csv"
            with path.open("w",newline="",encoding="utf-8") as handle:
                writer=csv.DictWriter(handle,fieldnames=["timestamp","symbol","open","high","low","close","volume"]); writer.writeheader()
                for stamp in (
                    "2026-01-02T14:25:00+00:00", "2026-01-02T14:30:00+00:00",
                    "2026-01-02T21:00:00+00:00", "2026-07-02T13:25:00+00:00",
                    "2026-07-02T13:30:00+00:00", "2026-07-02T20:00:00+00:00",
                ):
                    writer.writerow({"timestamp":stamp,"symbol":"TEST","open":10,"high":11,"low":9,"close":10,"volume":100})
            sessions=load_sessions(path)
            kept=[bar["timestamp"] for symbols in sessions.values() for bars in symbols.values() for bar in bars]
            self.assertEqual(["2026-01-02T14:30:00+00:00","2026-07-02T13:30:00+00:00"],kept)
            self.assertTrue(all(isinstance(bar,IntradayBar) for symbols in sessions.values() for bars in symbols.values() for bar in bars))

    def test_disk_session_store_matches_in_memory_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=root/"bars.csv"; database=root/"sessions.sqlite"
            path.write_text(
                "timestamp,symbol,open,high,low,close,volume,vwap\n"
                "2026-07-02T13:25:00+00:00,AAA,9,10,8,9,50,9\n"
                "2026-07-02T13:30:00+00:00,BBB,19,20,18,19,75,19\n"
                "2026-07-02T13:30:00+00:00,AAA,10,11,9,10,100,10\n",
                encoding="utf-8",
            )
            expected=load_sessions(path); actual=load_sessions(path,storage_path=database)
            try:
                self.assertEqual(list(expected),list(actual))
                self.assertEqual(set(expected["2026-07-02"]),set(actual["2026-07-02"]))
                self.assertEqual(expected["2026-07-02"]["AAA"],actual["2026-07-02"]["AAA"])
            finally:
                actual.close()
            self.assertFalse(database.exists())

    def test_rejects_unknown_strategy(self):
        with self.assertRaisesRegex(ValueError, "unknown intraday strategies"):
            IntradayConfig(strategy_names=("COMBINED_MAGIC",))

    def test_rejects_inverted_fundamental_bounds(self):
        with self.assertRaisesRegex(ValueError,"bounds are inverted"):
            IntradayConfig(minimum_market_cap=2_000,maximum_market_cap=1_000)

    def test_parameter_stability_restores_completed_variants(self):
        config=IntradayConfig(strategy_names=("OPENING_RANGE_BREAKOUT",))
        restored=[{"strategy":"OPENING_RANGE_BREAKOUT","variant":variant,"parameter":"opening_range_bars","value":value,"accepted_trades":1,"portfolio_return":.01,"mean_trade_return":.01,"win_rate":1.0} for variant,value in (("opening_range_bars=2",2),("opening_range_bars=4",4))]
        rows=_parameter_stability(config,{}, {},[],initial_checkpoint={"stability_rows":restored})
        self.assertEqual(3,len(rows))
        self.assertEqual({"baseline","opening_range_bars=2","opening_range_bars=4"},{row["variant"] for row in rows})

    def test_parameter_stability_discards_invalid_checkpoint_rows(self):
        config=IntradayConfig(strategy_names=("OPENING_RANGE_BREAKOUT",))
        invalid={"stability_rows":[{"strategy":"OTHER","variant":"bad","portfolio_return":999}]}
        callbacks=[]
        rows=_parameter_stability(config,{}, {},[],initial_checkpoint=invalid,checkpoint_callback=callbacks.append)
        self.assertEqual(3,len(rows))
        self.assertEqual(2,len(callbacks))

    def test_tournament_keeps_strategies_separate_and_uses_next_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); bars_path=root/"bars.csv"; archive=root/"result.zip"
            fields=["timestamp","symbol","open","high","low","close","volume","vwap"]
            with bars_path.open("w",newline="",encoding="utf-8") as handle:
                writer=csv.DictWriter(handle,fieldnames=fields); writer.writeheader(); start=datetime(2026,1,2,14,30,tzinfo=timezone.utc)
                for session in range(12):
                    for index in range(30):
                        price=10+index*(.01 if session<11 else .08)
                        writer.writerow({"timestamp":(start+timedelta(days=session,minutes=5*index)).isoformat(),"symbol":"TEST","open":price,"high":price+.04,"low":price-.02,"close":price+.02,"volume":100000 if session<11 else 400000,"vwap":price})
            job={"experiment":{"hypothesis_id":"INTRADAY_STRATEGY_ISOLATION","trial_id":"test"},"intraday_config":{"minimum_average_daily_dollar_volume":1,"target_notional":1,"strategy_names":["OPENING_RANGE_BREAKOUT"],"security_master_available":True,"halt_luld_available":True,"point_in_time_market_cap_available":True,"point_in_time_float_available":True}}
            run_tournament(job,bars_path,archive,hashlib.sha256(bars_path.read_bytes()).hexdigest())
            with zipfile.ZipFile(archive) as bundle:
                manifest=json.loads(bundle.read("intraday_manifest.json")); trades=bundle.read("intraday_trades.csv").decode()
                stability=list(csv.DictReader(bundle.read("intraday_parameter_stability.csv").decode().splitlines()))
                stability_summary=list(csv.DictReader(bundle.read("intraday_parameter_stability_summary.csv").decode().splitlines()))
            self.assertFalse(manifest["strategies_combined"])
            self.assertIn("OPENING_RANGE_BREAKOUT",trades)
            self.assertIn("entry_timestamp",trades)
            self.assertEqual({"baseline","opening_range_bars=2","opening_range_bars=4"},{row["variant"] for row in stability})
            self.assertIn("return_delta_vs_baseline",stability[0])
            self.assertEqual("OPENING_RANGE_BREAKOUT",stability_summary[0]["strategy"])
            self.assertEqual("2",stability_summary[0]["neighbor_count"])
            self.assertFalse(manifest["promotion_gate"]["checks"]["point_in_time_security_master"])
            self.assertFalse(manifest["promotion_gate"]["checks"]["halt_luld_available"])
            self.assertFalse(manifest["promotion_gate"]["checks"]["point_in_time_market_cap"])
            self.assertEqual([],list(root.glob("*.intraday-spill")))
            self.assertEqual([],list(root.glob("*.sessions.sqlite")))


if __name__ == "__main__": unittest.main()
