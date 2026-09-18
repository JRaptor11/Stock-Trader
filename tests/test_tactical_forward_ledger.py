import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from research.tactical_forward_ledger import append_observations


def _csv(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader(); writer.writerows(rows)
    return stream.getvalue()


class TacticalForwardLedgerTests(unittest.TestCase):
    def _fixtures(self, root):
        archive, declaration, ledger = root / "result.zip", root / "declaration.json", root / "ledger.jsonl"
        declaration.write_text(json.dumps({
            "forward_start": "2026-09-18", "state_confirmation_sessions": 3,
            "forward_tactical_hypotheses": [{
                "hypothesis_id": "H1", "strategy": "SECTOR_PARTICIPATION_BREAKOUT",
                "core_state": "BEAR_RECOVERING__HIGH_VOL__BROAD_BREADTH",
                "horizon_sessions": 2, "transition_phase": "pending",
            }],
        }))
        rows = [{
            "strategy": "SECTOR_PARTICIPATION_BREAKOUT", "cost_bps": 10,
            "signal_date": "2026-09-17", "entry_date": "2026-09-18",
            "comparison_end_date": "2026-09-22", "horizon_sessions": 2,
            "core_state": "BEAR_RECOVERING__HIGH_VOL__BROAD_BREADTH",
            "pending_core_state_at_entry": "NEXT", "tactical_net_return": .03,
            "baseline_return": .01, "incremental_return_vs_baseline": .02,
            "tactical_beat_baseline": "True",
        }]
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("tier1_manifest.json", json.dumps({"config": {"fixed": 1}}))
            bundle.writestr("tier1_market_state_definition.json", json.dumps({
                "state_change_confirmation_sessions": 3,
            }))
            bundle.writestr("tier1_tactical_horizon_comparisons.csv", _csv(rows))
        return archive, declaration, ledger

    def test_appends_matured_forward_event_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, declaration, ledger = self._fixtures(Path(directory))
            result = append_observations(archive, declaration, ledger)
            self.assertEqual(1, result["new_rows"])
            self.assertEqual("unchanged", append_observations(
                archive, declaration, ledger)["status"])

    def test_rejects_wrong_state_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, declaration, ledger = self._fixtures(Path(directory))
            payload = json.loads(declaration.read_text())
            payload["state_confirmation_sessions"] = 5
            declaration.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "state confirmation differs"):
                append_observations(archive, declaration, ledger)


if __name__ == "__main__":
    unittest.main()
