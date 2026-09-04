import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from research.tier1_forward_ledger import append_observation


class ForwardLedgerTests(unittest.TestCase):
    def _archive(self,path,equity=101000,config=None):
        manifest={"config":config or {"primary_cost_bps":10,"initial_cash":100000},"source_sha256":"source"}
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

    def test_additive_manifest_schema_does_not_change_frozen_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); old=root/"old.zip"; new=root/"new.zip"; ledger=root/"ledger.jsonl"
            frozen={"primary_cost_bps":10,"initial_cash":100000,"rebalance_frequency":"monthly"}
            self._archive(old,config=frozen)
            append_observation(old,ledger,"2026-09-03")
            self._archive(new,equity=102000,config={**frozen,"minimum_common_coverage_pct":99.0})
            result=append_observation(new,ledger,"2026-09-03")
            self.assertEqual("unchanged",result["status"])

    def test_changed_frozen_field_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); old=root/"old.zip"; new=root/"new.zip"; ledger=root/"ledger.jsonl"
            self._archive(old,config={"primary_cost_bps":10,"initial_cash":100000,"rebalance_frequency":"monthly"})
            append_observation(old,ledger,"2026-09-03")
            self._archive(new,config={"primary_cost_bps":10,"initial_cash":100000,"rebalance_frequency":"weekly"})
            with self.assertRaisesRegex(ValueError,"configuration changed"):
                append_observation(new,ledger,"2026-09-03")


if __name__ == "__main__": unittest.main()
