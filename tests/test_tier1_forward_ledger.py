import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from research.tier1_forward_ledger import append_observation


class ForwardLedgerTests(unittest.TestCase):
    def _archive(self,path,equity=101000):
        manifest={"config":{"primary_cost_bps":10,"initial_cash":100000},"source_sha256":"source"}
        stream="date,strategy,cost_bps,equity,cash,positions\n2026-09-02,SECTOR_ETF_ROTATION,10.0,100000,0,3\n2026-09-03,SECTOR_ETF_ROTATION,10.0,%s,0,3\n2026-09-02,STATIC_MULTI_SLEEVE,10.0,100000,0,8\n2026-09-03,STATIC_MULTI_SLEEVE,10.0,102000,0,8\n"%equity
        with zipfile.ZipFile(path,"w") as z: z.writestr("tier1_manifest.json",json.dumps(manifest)); z.writestr("tier1_daily.csv",stream)

    def test_append_is_idempotent_and_hash_chained(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); archive=root/"result.zip"; ledger=root/"ledger.jsonl"; self._archive(archive)
            first=append_observation(archive,ledger,"2026-09-03"); second=append_observation(archive,ledger,"2026-09-03")
            self.assertEqual("appended",first["status"]); self.assertEqual("unchanged",second["status"])
            self.assertEqual(1,len(ledger.read_text().splitlines())); self.assertTrue(first["chain_sha256"])

    def test_append_supports_named_shadow_strategy(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); archive=root/"result.zip"; ledger=root/"ledger.jsonl"; self._archive(archive)
            result=append_observation(archive,ledger,"2026-09-03","STATIC_MULTI_SLEEVE","tier2-static-v1")
            self.assertEqual("STATIC_MULTI_SLEEVE",result["strategy"])
            self.assertEqual("tier2-static-v1",result["variant"])
            self.assertAlmostEqual(.02,result["daily_return"])


if __name__ == "__main__": unittest.main()
