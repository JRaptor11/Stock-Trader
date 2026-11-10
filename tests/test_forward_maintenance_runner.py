import json
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from research.forward_maintenance_runner import (
    Journal,
    RunLock,
    _make_job,
    _merge_nonoverlapping_csv,
    _retry_network,
    build_calendar_plan,
    require_free_space,
    validate_preflight,
)


EASTERN = ZoneInfo("America/New_York")
HEADER = "timestamp,symbol,open,high,low,close,volume,trade_count,vwap\n"


class ForwardMaintenanceRunnerTests(unittest.TestCase):
    def test_calendar_plan_uses_latest_close_and_prior_completed_week(self):
        rows = [
            {"date": day, "open": "09:30", "close": "16:00"}
            for day in (
                "2026-10-30", "2026-11-02", "2026-11-03",
                "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09",
            )
        ]
        plan = build_calendar_plan(
            rows,
            datetime(2026, 11, 3, 17, 0, tzinfo=EASTERN),
        )
        self.assertEqual("2026-11-03", plan.latest_completed)
        self.assertEqual("2026-11-04", plan.next_session)
        self.assertEqual("2026-10-30", plan.weekly_required)
        self.assertFalse(plan.latest_is_week_final)

    def test_calendar_plan_recognizes_holiday_shortened_week(self):
        rows = [
            {"date": day, "open": "09:30", "close": close}
            for day, close in (
                ("2026-11-20", "16:00"),
                ("2026-11-23", "16:00"),
                ("2026-11-24", "16:00"),
                ("2026-11-25", "16:00"),
                ("2026-11-27", "13:00"),
                ("2026-11-30", "16:00"),
            )
        ]
        plan = build_calendar_plan(
            rows,
            datetime(2026, 11, 27, 14, 0, tzinfo=EASTERN),
        )
        self.assertEqual("2026-11-27", plan.latest_completed)
        self.assertEqual("2026-11-30", plan.next_session)
        self.assertEqual("2026-11-27", plan.weekly_required)
        self.assertTrue(plan.latest_is_week_final)

    def test_preflight_allows_lag_but_rejects_integrity_failures(self):
        lag = {
            "broker_orders_enabled": False,
            "service": {"errors": [
                "remote shadow is at 2026-11-02; required 2026-11-03"
            ]},
            "ledgers": [{
                "id": "daily",
                "errors": ["latest session is 2026-11-02; required 2026-11-03"],
            }],
        }
        validate_preflight(lag)
        lag["ledgers"][0]["errors"].append("row 3 has an invalid record hash")
        with self.assertRaisesRegex(RuntimeError, "invalid record hash"):
            validate_preflight(lag)

    def test_lock_rejects_overlap_and_recovers_dead_stale_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "maintenance.lock"
            with RunLock(path):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with RunLock(path):
                        self.fail("overlapping lock unexpectedly succeeded")
            path.write_text(json.dumps({
                "pid": 99999999,
                "created_at": "2000-01-01T00:00:00+00:00",
            }), encoding="utf-8")
            with RunLock(path):
                self.assertTrue(path.is_file())
            self.assertFalse(path.exists())

    def test_merge_requires_strict_nonoverlap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "base.csv"
            incremental = root / "incremental.csv"
            output = root / "output.csv"
            base.write_text(
                HEADER + "2026-11-02T05:00:00Z,SPY,1,1,1,1,1,1,1\n",
                encoding="utf-8",
            )
            incremental.write_text(
                HEADER + "2026-11-03T05:00:00Z,SPY,2,2,2,2,2,2,2\n",
                encoding="utf-8",
            )
            _merge_nonoverlapping_csv(base, incremental, output)
            self.assertIn("2026-11-03", output.read_text(encoding="utf-8"))
            incremental.write_text(
                HEADER + "2026-11-02T05:00:00Z,SPY,2,2,2,2,2,2,2\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "overlap"):
                _merge_nonoverlapping_csv(base, incremental, output)

    def test_journal_records_failure_without_traceback_or_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "last-run.json"
            journal = Journal(path)
            journal.stage("preflight", guard_ok=False)
            journal.fail(RuntimeError("expected failure"))
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("failed", document["status"])
            self.assertEqual("RuntimeError", document["error_type"])
            self.assertFalse(document["broker_orders_enabled"])
            self.assertNotIn("traceback", document)

    def test_disk_guard_fails_before_work_below_reserve(self):
        usage = type("Usage", (), {"free": 99})()
        with patch(
            "research.forward_maintenance_runner.shutil.disk_usage",
            return_value=usage,
        ):
            with self.assertRaisesRegex(RuntimeError, "insufficient local disk space"):
                require_free_space(Path("."), 100)
            self.assertEqual(99, require_free_space(Path("."), 99)["free_bytes"])

    def test_network_retry_is_bounded_and_recovers(self):
        attempts = []

        def operation():
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                raise urllib.error.URLError("temporary")
            return "ok"

        with patch("research.forward_maintenance_runner.time_module.sleep") as sleep:
            self.assertEqual("ok", _retry_network("test", operation, attempts=4))
        self.assertEqual([1, 2, 3], attempts)
        self.assertEqual(2, sleep.call_count)

    def test_compact_job_profile_is_explicit_and_uses_core_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.json"
            destination = root / "job.json"
            source.write_text(
                json.dumps({"tier1_config": {"primary_cost_bps": 10}}),
                encoding="utf-8",
            )
            _make_job(
                source,
                destination,
                job_id="test",
                bars_csv="bars.csv",
                data_source="test",
                artifact_profile="forward_ledger",
            )
            document = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual("forward_ledger", document["artifact_profile"])
            self.assertEqual(
                "baseline_core", document["tier1_config"]["diagnostic_profile"]
            )


if __name__ == "__main__":
    unittest.main()
