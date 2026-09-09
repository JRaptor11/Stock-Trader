import csv, json, tempfile, unittest, zipfile
from pathlib import Path

from research.aggregate_intraday_shards import aggregate
from research.shard_intraday_csv import build_jobs, shard_csv


class IntradayShardTests(unittest.TestCase):
    @staticmethod
    def _write_archive(path, start, end, trades=(), conditions=()):
        config={"strategy_names":["OPENING_RANGE_BREAKOUT"],"cost_bps_per_side":10,"initial_cash":100000,"target_notional":10000,"maximum_positions":1,"maximum_symbol_pct":1,"walk_forward_train_sessions":2,"walk_forward_test_sessions":1,"walk_forward_step_sessions":1}
        with zipfile.ZipFile(path,"w") as bundle:
            bundle.writestr("intraday_manifest.json",json.dumps({"experiment":{"hypothesis_id":"INTRADAY_STRATEGY_ISOLATION"},"evaluation_range":{"start":start,"end":end},"config":config}))
            for name,rows in (("intraday_trades.csv",trades),("intraday_market_conditions.csv",conditions)):
                if rows:
                    stream=[]; fields=list(rows[0]); stream.append(",".join(fields)); stream.extend(",".join(str(row.get(field,"")) for field in fields) for row in rows); bundle.writestr(name,"\n".join(stream)+"\n")
                else: bundle.writestr(name,"")
            for name in ("intraday_signals.csv","intraday_matched_controls.csv","intraday_portfolio_events.csv","intraday_parameter_stability.csv"): bundle.writestr(name,"")

    def test_shards_have_nonoverlapping_evaluation_and_warmup(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"bars.csv"
            with source.open("w",newline="",encoding="utf-8") as handle:
                writer=csv.DictWriter(handle,fieldnames=["timestamp","symbol","open","high","low","close","volume"]); writer.writeheader()
                for day in range(1,7): writer.writerow({"timestamp":f"2026-01-{day:02d}T14:30:00Z","symbol":"AAA","open":1,"high":1,"low":1,"close":1,"volume":1})
            manifest=json.loads(shard_csv(source,root/"shards",evaluation_sessions=2,warmup_sessions=2).read_text())
            self.assertEqual(3,len(manifest["shards"]))
            self.assertEqual("2026-01-01",manifest["shards"][1]["warmup_start"])
            self.assertEqual("2026-01-03",manifest["shards"][1]["evaluation_start"])
            with (root/"shards"/manifest["shards"][1]["filename"]).open() as handle:
                self.assertEqual(4,len(list(csv.DictReader(handle))))

    def test_jobs_are_built_only_from_predeclared_shards(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); template=root/"intraday-strategy-generation-001-job.json"; manifest=root/"shards.json"
            template.write_text(json.dumps({"job_id":"template","bars_csv":"source.csv","experiment":{"trial_id":"template"},"intraday_config":{}}))
            template.with_name("intraday-strategy-generation-001-shards.json").write_text(json.dumps({"shards":[{"job_id":"job-001","filename":"part.csv"}]}))
            manifest.write_text(json.dumps({"shards":[{"number":1,"filename":"part.csv","evaluation_start":"2026-01-01","evaluation_end":"2026-01-31"}]}))
            paths=build_jobs(template,manifest,root,implementation_commit="abc123")
            job=json.loads(paths[0].read_text())
            self.assertEqual("job-001",job["job_id"]); self.assertEqual("abc123",job["implementation_commit"])
            self.assertEqual("2026-01-01",job["intraday_config"]["evaluation_start_date"])

    def test_aggregate_rejects_overlapping_evaluation_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); archives=[]
            for number,start,end in ((1,"2026-01-01","2026-01-03"),(2,"2026-01-03","2026-01-05")):
                path=root/f"{number}.zip"; archives.append(path)
                with zipfile.ZipFile(path,"w") as bundle:
                    bundle.writestr("intraday_manifest.json",json.dumps({"experiment":{"hypothesis_id":"INTRADAY_STRATEGY_ISOLATION"},"evaluation_range":{"start":start,"end":end},"config":{"strategy_names":["OPENING_RANGE_BREAKOUT"],"cost_bps_per_side":10,"initial_cash":100000,"walk_forward_train_sessions":2,"walk_forward_test_sessions":1,"walk_forward_step_sessions":1}}))
                    for name in ("intraday_signals.csv","intraday_trades.csv","intraday_matched_controls.csv","intraday_portfolio_events.csv","intraday_market_conditions.csv"): bundle.writestr(name,"")
            with self.assertRaisesRegex(ValueError,"overlap"): aggregate(archives,root/"aggregate.zip")

    def test_aggregate_filters_warmup_and_reallocates_across_shards(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); first=root/"first.zip"; second=root/"second.zip"
            base={"symbol":"AAA","strategy":"OPENING_RANGE_BREAKOUT","exit_timestamp":"2026-01-01T15:00:00+00:00","net_return":-0.01,"portfolio_status":"accepted","allocated_notional":10000,"realized_pnl":-100}
            self._write_archive(first,"2026-01-01","2026-01-01",[{**base,"date":"2026-01-01","entry_timestamp":"2026-01-01T14:35:00+00:00"}],[{"date":"2026-01-01","state":"one"}])
            self._write_archive(second,"2026-01-02","2026-01-02",[{**base,"date":"2026-01-02","entry_timestamp":"2026-01-02T14:35:00+00:00","exit_timestamp":"2026-01-02T15:00:00+00:00","portfolio_status":"rejected","realized_pnl":0}],[{"date":"2026-01-01","state":"warmup"},{"date":"2026-01-02","state":"two"}])
            output=aggregate([first,second],root/"aggregate.zip")
            with zipfile.ZipFile(output) as bundle:
                trades=list(csv.DictReader(bundle.read("intraday_trades.csv").decode().splitlines()))
                conditions=list(csv.DictReader(bundle.read("intraday_market_conditions.csv").decode().splitlines()))
                manifest=json.loads(bundle.read("intraday_aggregate_manifest.json"))
                required={"intraday_daily.csv","intraday_performance.csv","intraday_cost_sensitivity.csv",
                          "intraday_block_bootstrap.csv","intraday_condition_scorecards.csv",
                          "intraday_condition_pair_scorecards.csv","intraday_nested_regime_walk_forward.csv"}
                self.assertTrue(required.issubset(bundle.namelist()))
            self.assertEqual(["accepted","accepted"],[row["portfolio_status"] for row in trades])
            self.assertEqual(["2026-01-01","2026-01-02"],[row["date"] for row in conditions])
            self.assertIn("reconstructed chronologically",manifest["portfolio_accounting"])


if __name__=="__main__": unittest.main()
