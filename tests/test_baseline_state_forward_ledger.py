import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from research.baseline_state_forward_ledger import append_observations


def _csv(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader(); writer.writerows(rows)
    return stream.getvalue()


class BaselineStateForwardLedgerTests(unittest.TestCase):
    def _fixtures(self, root):
        archive = root / "result.zip"
        declaration = root / "declaration.json"
        ledger = root / "ledger.jsonl"
        declaration.write_text(json.dumps({
            "forward_start": "2026-09-04", "benchmark_strategy": "SPY_BUY_HOLD",
            "primary_cost_bps": 10, "state_confirmation_sessions": 3,
            "forward_baseline_hypotheses": [{
                "hypothesis_id": "factor-accelerating", "strategy": "FACTOR_ETF_MOMENTUM",
                "core_state": "BULL_ACCELERATING__LOW_VOL__BROAD_BREADTH",
            }],
        }), encoding="utf-8")
        daily = []
        for strategy, equities in (("SPY_BUY_HOLD", (100, 101, 102)),
                                   ("FACTOR_ETF_MOMENTUM", (100, 102, 105))):
            daily.extend({"date": day, "strategy": strategy, "cost_bps": 10, "equity": equity}
                         for day, equity in zip(("2026-09-03", "2026-09-04", "2026-09-08"), equities))
        labels = [
            {"date": "2026-09-03", "core_state": "OTHER", "state_changed": False,
             "pending_core_state": ""},
            {"date": "2026-09-04", "core_state": "BULL_ACCELERATING__LOW_VOL__BROAD_BREADTH",
             "state_changed": True, "pending_core_state": ""},
            {"date": "2026-09-08", "core_state": "OTHER", "state_changed": False,
             "pending_core_state": "TARGET"},
        ]
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("tier1_manifest.json", json.dumps({"config": {"fixed": 1}}))
            bundle.writestr("tier1_market_state_definition.json", json.dumps({
                "state_change_confirmation_sessions": 3,
            }))
            bundle.writestr("tier1_daily.csv", _csv(daily))
            bundle.writestr("tier1_market_state_labels.csv", _csv(labels))
        return archive, declaration, ledger

    def test_appends_all_forward_sessions_and_compounds_only_active_state(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, declaration, ledger = self._fixtures(Path(directory))
            result = append_observations(archive, declaration, ledger)
            self.assertEqual(2, result["new_rows"])
            self.assertEqual(1, result["active_new_rows"])
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertTrue(rows[0]["state_active"])
            self.assertFalse(rows[1]["state_active"])
            self.assertEqual(rows[0]["active_strategy_compounded_return"],
                             rows[1]["active_strategy_compounded_return"])
            self.assertEqual("unchanged", append_observations(
                archive, declaration, ledger)["status"])

    def test_fails_closed_if_frozen_declaration_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, declaration, ledger = self._fixtures(Path(directory))
            append_observations(archive, declaration, ledger)
            payload = json.loads(declaration.read_text())
            payload["forward_start"] = "2026-09-08"
            declaration.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "declaration or configuration changed"):
                append_observations(archive, declaration, ledger)


if __name__ == "__main__":
    unittest.main()
