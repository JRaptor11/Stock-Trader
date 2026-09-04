import csv
import io
import hashlib
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from research.app import app, runtime, _upload_status_snapshot
except ImportError:
    TestClient = None
    app = None
    runtime = None
    _upload_status_snapshot = None


class _SnapshotStore:
    durable = True

    def __init__(self, source: Path, *, fail=False):
        self.source = source
        self.fail = fail
        self.uploaded = None

    def upload_file(self, path, _key):
        # Simulate the live worker replacing the original status while the
        # coordinator uploads. The immutable snapshot must remain unchanged.
        self.source.write_text('{"status":"newer"}', encoding="utf-8")
        if self.fail:
            raise RuntimeError("transient upload failure")
        self.uploaded = Path(path).read_bytes()


@unittest.skipIf(_upload_status_snapshot is None, "FastAPI dependencies are not installed")
class StatusSnapshotTests(unittest.TestCase):
    def test_status_upload_uses_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "job.status.json"
            original = b'{"status":"running"}'
            status_path.write_bytes(original)
            store = _SnapshotStore(status_path)
            with patch.object(runtime, "store", store):
                digest = _upload_status_snapshot(
                    status_path, durable_key="status/job.status.json"
                )
            self.assertEqual(original, store.uploaded)
            self.assertEqual(hashlib.sha256(original).hexdigest(), digest)

    def test_status_upload_failure_is_nonfatal_and_retriable(self):
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "job.status.json"
            status_path.write_text('{"status":"running"}', encoding="utf-8")
            store = _SnapshotStore(status_path, fail=True)
            with patch.object(runtime, "store", store):
                digest = _upload_status_snapshot(
                    status_path, durable_key="status/job.status.json",
                    previous_digest="previous",
                )
            self.assertEqual("previous", digest)


@unittest.skipIf(TestClient is None, "FastAPI test dependencies are not installed")
class ResearchAppTests(unittest.TestCase):
    token = "test-research-token-at-least-24-characters"

    def _bars(self) -> bytes:
        stream = io.StringIO()
        fields = ["timestamp", "symbol", "open", "high", "low", "close", "volume", "trade_count", "vwap"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
        for index in range(76):
            for symbol, offset in (("AAA", 0.0), ("BBB", 10.0)):
                price = 100.0 + offset + index * 0.05
                writer.writerow({
                    "timestamp": (start + timedelta(minutes=5 * index)).isoformat(),
                    "symbol": symbol, "open": price, "high": price + 0.1,
                    "low": price - 0.1, "close": price + 0.02,
                    "volume": 1000, "trade_count": 100, "vwap": price,
                })
        return stream.getvalue().encode("utf-8")

    def test_authenticated_dataset_job_and_download_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                "SERVICE_MODE": "historical_research",
                "BROKER_EXECUTION_ENABLED": "false",
                "RESEARCH_API_TOKEN": self.token,
                "RESEARCH_DATA_DIR": str(root / "data"),
                "RESEARCH_JOB_DIR": str(root / "jobs"),
                "RESEARCH_RESULTS_DIR": str(root / "results"),
            }
            with patch.dict(os.environ, env, clear=False), TestClient(app) as client:
                self.assertEqual(client.get("/healthz").status_code, 200)
                self.assertEqual(client.get("/api/public/uptime-health").status_code, 200)
                self.assertEqual(client.head("/api/public/uptime-health").status_code, 200)
                self.assertEqual(client.get("/api/jobs").status_code, 401)
                headers = {"Authorization": f"Bearer {self.token}"}
                uploaded = client.put(
                    "/api/datasets/bars.csv", content=self._bars(), headers=headers,
                )
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                submitted = client.post("/api/jobs", headers=headers, json={
                    "job_id": "api-smoke", "bars_csv": "bars.csv",
                    "replay_config": {
                        "warmup_bars": 61, "min_train_sessions": 1,
                        "test_sessions": 1, "require_benchmark": False,
                    },
                })
                self.assertEqual(submitted.status_code, 202, submitted.text)
                # Windows CI and instrumented replay builds can spend more than
                # ten seconds finalizing diagnostics. Never tear down the test
                # directory while the subprocess still owns spill files.
                deadline = time.monotonic() + 60
                payload = {}
                while time.monotonic() < deadline:
                    response = client.get("/api/jobs/api-smoke", headers=headers)
                    if response.status_code == 200:
                        payload = response.json()
                        if payload.get("status") in {"complete", "failed"} and payload.get("archive"):
                            break
                    time.sleep(0.05)
                self.assertEqual(payload.get("status"), "complete", payload)
                downloaded = client.get("/api/jobs/api-smoke/download", headers=headers)
                self.assertEqual(downloaded.status_code, 200)
                self.assertTrue(downloaded.content.startswith(b"PK"))
                coordinator_deadline=time.monotonic()+10
                while runtime.active_job_id is not None and time.monotonic()<coordinator_deadline:
                    time.sleep(.05)
                self.assertIsNone(runtime.active_job_id,"coordinator did not finish final status handling")
            runtime.active_job_id = None

    def test_research_app_refuses_execution_enabled(self):
        with patch.dict(os.environ, {
            "SERVICE_MODE": "historical_research",
            "BROKER_EXECUTION_ENABLED": "true",
            "RESEARCH_API_TOKEN": self.token,
        }, clear=False):
            with self.assertRaises(RuntimeError):
                runtime.initialize()
        runtime.active_job_id = None

    def test_supersede_retires_nonrunning_job_and_preserves_audit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                "SERVICE_MODE": "historical_research",
                "BROKER_EXECUTION_ENABLED": "false",
                "RESEARCH_API_TOKEN": self.token,
                "RESEARCH_DATA_DIR": str(root / "data"),
                "RESEARCH_JOB_DIR": str(root / "jobs"),
                "RESEARCH_RESULTS_DIR": str(root / "results"),
            }
            with patch.dict(os.environ, env, clear=False), TestClient(app) as client:
                headers = {"Authorization": f"Bearer {self.token}"}
                status_path = runtime.results_root / "obsolete.status.json"
                status_path.write_text(json.dumps({
                    "job_id": "obsolete", "status": "retry_wait",
                    "blocks_queue": False,
                    "next_retry_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                }), encoding="utf-8")
                response = client.post("/api/jobs/obsolete/supersede", headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                payload = json.loads(status_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], "superseded")
                self.assertIsNone(payload["next_retry_at"])
                self.assertIn("superseded_at", payload)
            runtime.active_job_id = None

    def test_manual_retry_preserves_job_id_and_resets_automatic_retry_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                "SERVICE_MODE": "historical_research",
                "BROKER_EXECUTION_ENABLED": "false",
                "RESEARCH_API_TOKEN": self.token,
                "RESEARCH_DATA_DIR": str(root / "data"),
                "RESEARCH_JOB_DIR": str(root / "jobs"),
                "RESEARCH_RESULTS_DIR": str(root / "results"),
            }
            with patch.dict(os.environ, env, clear=False), TestClient(app) as client:
                headers = {"Authorization": f"Bearer {self.token}"}
                (runtime.job_root / "failed-job.json").write_text(json.dumps({
                    "job_id": "failed-job", "bars_csv": "bars.csv",
                }), encoding="utf-8")
                status_path = runtime.results_root / "failed-job.status.json"
                status_path.write_text(json.dumps({
                    "job_id": "failed-job", "status": "failed",
                    "attempt_count": 4, "blocks_queue": True,
                    "error": "restart allowance exhausted",
                    "failure_class": "interrupted_service_restart",
                    "queued_at": "2026-08-25T00:00:00+00:00",
                }), encoding="utf-8")
                with patch("research.app._start_next_job"):
                    response = client.post("/api/jobs/failed-job/retry", headers=headers)
                self.assertEqual(200, response.status_code, response.text)
                payload = json.loads(status_path.read_text(encoding="utf-8"))
                self.assertEqual("queued", payload["status"])
                self.assertEqual(0, payload["attempt_count"])
                self.assertEqual(4, payload["lifetime_attempt_count"])
                self.assertEqual(1, payload["manual_retry_count"])
                self.assertFalse(payload["blocks_queue"])
                self.assertEqual(runtime.maximum_retries, payload["retries_remaining"])
                self.assertEqual(
                    "restart allowance exhausted", payload["last_failure"]["error"]
                )
            runtime.active_job_id = None


if __name__ == "__main__":
    unittest.main()
