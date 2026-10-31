import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from research.forward_maintenance_guard import audit_ledger, build_report


def chained(previous, session, config="frozen"):
    row = {
        "session": session, "config_sha256": config,
        "previous_chain_sha256": previous, "broker_orders_enabled": False,
    }
    row["chain_sha256"] = hashlib.sha256(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return row


class ForwardMaintenanceGuardTests(unittest.TestCase):
    def test_valid_ledger_and_state_are_current(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = chained(None, "2026-10-29")
            second = chained(first["chain_sha256"], "2026-10-30")
            ledger = root / "ledger.jsonl"
            ledger.write_text("\n".join(json.dumps(row) for row in (first, second)) + "\n")
            (root / "state.json").write_text(json.dumps({
                "last_session": "2026-10-30", "chain_sha256": second["chain_sha256"],
                "broker_orders_enabled": False,
            }))
            result = audit_ledger({
                "id": "test", "path": "{workspace_root}/ledger.jsonl",
                "state_path": "{workspace_root}/state.json", "date_field": "session",
            }, root, "2026-10-30")
            self.assertTrue(result["ok"])

    def test_stale_or_tampered_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = chained(None, "2026-10-29")
            row["broker_orders_enabled"] = True
            (root / "ledger.jsonl").write_text(json.dumps(row) + "\n")
            result = audit_ledger({
                "id": "test", "path": "{workspace_root}/ledger.jsonl",
                "date_field": "session",
            }, root, "2026-10-30")
            self.assertFalse(result["ok"])
            self.assertGreaterEqual(len(result["errors"]), 3)

    def test_report_requires_remote_cursor_and_completed_readiness(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = chained(None, "2026-10-30")
            (root / "ledger.jsonl").write_text(json.dumps(row) + "\n")
            policy = {"policy_id": "TEST", "ledgers": [{
                "id": "test", "cadence": "daily", "date_field": "session",
                "path": "{workspace_root}/ledger.jsonl",
            }]}
            valid = build_report(policy, root, "2026-10-30", None, {
                "last_session": "2026-10-30", "expected_next_session": "2026-11-02",
                "broker_orders_enabled": False, "readiness": {"status": "completed"},
            })
            self.assertTrue(valid["ok"])
            invalid = build_report(policy, root, "2026-10-30", None, {
                "last_session": "2026-10-29", "broker_orders_enabled": False,
                "readiness": {"status": "required"},
            })
            self.assertFalse(invalid["ok"])


if __name__ == "__main__":
    unittest.main()
