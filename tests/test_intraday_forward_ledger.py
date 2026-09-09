import json, tempfile, unittest, zipfile
from pathlib import Path

from research.intraday_forward_ledger import append_observation


class IntradayForwardLedgerTests(unittest.TestCase):
    def _archive(self,path,cost=10):
        manifest={"source_sha256":"source","config":{"initial_cash":100000,"strategy_names":["S"],"cost_bps_per_side":cost}}
        with zipfile.ZipFile(path,"w") as bundle:
            bundle.writestr("intraday_manifest.json",json.dumps(manifest))
            bundle.writestr("intraday_daily.csv","date,strategy,equity,daily_return,daily_pnl,drawdown,turnover,trades\n2026-09-08,S,100100,.001,100,0,.2,1\n")
            bundle.writestr("intraday_benchmark_daily.csv","date,strategy,equity,daily_return\n2026-09-08,SPY_BUY_HOLD,100200,.002\n")

    def test_append_is_idempotent_and_hash_chained(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); archive=root/"result.zip"; ledger=root/"ledger.jsonl"; self._archive(archive)
            first=append_observation(archive,ledger,"2026-09-01","S"); second=append_observation(archive,ledger,"2026-09-01","S")
            self.assertEqual("appended",first["status"]); self.assertEqual("unchanged",second["status"])
            self.assertEqual(1,len(ledger.read_text().splitlines()))

    def test_changed_frozen_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); ledger=root/"ledger.jsonl"; first=root/"one.zip"; second=root/"two.zip"
            self._archive(first,10); self._archive(second,20); append_observation(first,ledger,"2026-09-01","S")
            with self.assertRaisesRegex(ValueError,"configuration changed"): append_observation(second,ledger,"2026-09-01","S")


if __name__=="__main__": unittest.main()
