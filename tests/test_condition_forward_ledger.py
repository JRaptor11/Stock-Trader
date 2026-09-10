import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from research.condition_forward_ledger import append_observations


def _csv(rows):
    output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows); return output.getvalue()


class ConditionForwardLedgerTests(unittest.TestCase):
    def _fixtures(self, root: Path):
        archive, declaration, ledger = root / "result.zip", root / "declaration.json", root / "ledger.jsonl"
        declaration.write_text(json.dumps({
            "frozen_at": "2026-09-10T00:00:00Z", "evidence_observed_through": "2026-09-03",
            "benchmark_strategy": "STATIC", "primary_cost_bps": 10,
            "hypotheses": [{"id": "H1", "strategy": "TEST", "dimension": "change_bucket", "bucket": "Q3"}],
        }))
        daily = []
        for strategy, equities in (("STATIC", (100, 101, 102)), ("TEST", (100, 102, 104))):
            daily.extend({"date": day, "strategy": strategy, "cost_bps": 10, "equity": equity}
                         for day, equity in zip(("2026-09-03", "2026-09-04", "2026-09-08"), equities))
        conditions = [
            {"date": "2026-09-03", "change": .1, "change_bucket": "Q2"},
            {"date": "2026-09-04", "change": .2, "change_bucket": "Q3"},
            {"date": "2026-09-08", "change": .3, "change_bucket": "Q4"},
        ]
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("tier1_manifest.json", json.dumps({"config": {"fixed": 1}}))
            bundle.writestr("tier1_daily.csv", _csv(daily)); bundle.writestr("tier1_market_conditions.csv", _csv(conditions))
        return archive, declaration, ledger

    def test_appends_each_unseen_forward_session_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, declaration, ledger = self._fixtures(Path(directory))
            result = append_observations(archive, declaration, ledger)
            self.assertEqual(2, result["new_rows"]); self.assertEqual(1, result["active_new_rows"])
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertEqual(["2026-09-04", "2026-09-08"], [row["as_of_date"] for row in rows])
            self.assertTrue(rows[0]["condition_active"]); self.assertEqual(rows[0]["chain_sha256"], rows[1]["previous_chain_sha256"])
            self.assertEqual("unchanged", append_observations(archive, declaration, ledger)["status"])

    def test_fails_closed_when_declaration_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, declaration, ledger = self._fixtures(Path(directory)); append_observations(archive, declaration, ledger)
            payload = json.loads(declaration.read_text()); payload["hypotheses"][0]["bucket"] = "Q4"; declaration.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "declaration changed"):
                append_observations(archive, declaration, ledger)

    def test_fails_closed_when_existing_chain_is_tampered(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, declaration, ledger = self._fixtures(Path(directory)); append_observations(archive, declaration, ledger)
            rows = ledger.read_text().splitlines(); payload = json.loads(rows[0]); payload["daily_excess"] = 999; rows[0] = json.dumps(payload)
            ledger.write_text("\n".join(rows) + "\n")
            with self.assertRaisesRegex(ValueError, "row hash is invalid"):
                append_observations(archive, declaration, ledger)


if __name__ == "__main__": unittest.main()
