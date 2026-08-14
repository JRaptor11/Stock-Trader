import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from research.worker import _resolved_child, execute_job


class ResearchWorkerTests(unittest.TestCase):
    def _write_bars(self, path: Path) -> None:
        fields = ["timestamp", "symbol", "open", "high", "low", "close", "volume", "trade_count", "vwap"]
        start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for index in range(76):
                timestamp = start + timedelta(minutes=5 * index)
                for symbol, offset in (("AAA", 0.0), ("BBB", 10.0)):
                    price = 100.0 + offset + index * 0.05
                    writer.writerow({
                        "timestamp": timestamp.isoformat(), "symbol": symbol,
                        "open": price, "high": price + 0.1, "low": price - 0.1,
                        "close": price + 0.02, "volume": 1000 + index,
                        "trade_count": 100 + index, "vwap": price,
                    })

    def test_job_runs_in_isolated_roots_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, results = root / "data", root / "results"
            data.mkdir()
            bars = data / "bars.csv"
            self._write_bars(bars)
            job = root / "experiment.json"
            job.write_text(json.dumps({
                "job_id": "smoke-test", "bars_csv": "bars.csv",
                "replay_config": {"warmup_bars": 61, "min_train_sessions": 1, "test_sessions": 1},
            }), encoding="utf-8")
            env = {
                "SERVICE_MODE": "historical_research",
                "BROKER_EXECUTION_ENABLED": "false",
                "GIT_COMMIT": "test-commit",
            }
            with patch.dict(os.environ, env, clear=False):
                output = execute_job(job, data, results)
            manifest = json.loads((output / "replay_manifest.json").read_text(encoding="utf-8"))
            status = json.loads((results / "smoke-test.status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "complete")
            self.assertEqual(manifest["service_mode"], "historical_research")
            self.assertEqual(manifest["git_commit"], "test-commit")
            self.assertEqual(manifest["experiment"]["job_id"], "smoke-test")

    def test_worker_rejects_trading_mode_before_running_job(self):
        with patch.dict(os.environ, {
            "SERVICE_MODE": "paper_trading",
            "BROKER_EXECUTION_ENABLED": "true",
        }, clear=False):
            with self.assertRaises(RuntimeError):
                execute_job("missing.json", ".", ".")

    def test_paths_cannot_escape_configured_root(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                _resolved_child(Path(directory), "../outside.csv")


if __name__ == "__main__":
    unittest.main()
