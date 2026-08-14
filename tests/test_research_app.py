import csv
import io
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from research.app import app, runtime
except ImportError:
    TestClient = None
    app = None
    runtime = None


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
                self.assertEqual(client.get("/api/jobs").status_code, 401)
                headers = {"Authorization": f"Bearer {self.token}"}
                uploaded = client.put(
                    "/api/datasets/bars.csv", content=self._bars(), headers=headers,
                )
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                submitted = client.post("/api/jobs", headers=headers, json={
                    "job_id": "api-smoke", "bars_csv": "bars.csv",
                    "replay_config": {"warmup_bars": 61, "min_train_sessions": 1, "test_sessions": 1},
                })
                self.assertEqual(submitted.status_code, 202, submitted.text)
                deadline = time.monotonic() + 10
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


if __name__ == "__main__":
    unittest.main()
