import csv
import json
import os
import tempfile
import unittest
import zipfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from research.worker import (
    _resolved_child, _restore_checkpoint_bundle,
    _restore_incremental_checkpoint, _write_checkpoint_bundle,
    _write_incremental_checkpoint_part, _write_json_atomic, execute_job,
    _load_intraday_checkpoint,
)


class ResearchWorkerTests(unittest.TestCase):
    def test_atomic_json_writes_do_not_share_temporary_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"status.json"; barrier=threading.Barrier(3); errors=[]
            def write(value):
                try: barrier.wait(); _write_json_atomic(path,{"value":value})
                except Exception as exc: errors.append(exc)
            threads=[threading.Thread(target=write,args=(value,)) for value in (1,2)]
            for thread in threads: thread.start()
            barrier.wait()
            for thread in threads: thread.join()
            self.assertEqual([],errors)
            self.assertIn(json.loads(path.read_text(encoding="utf-8"))["value"],(1,2))
            self.assertEqual([],list(Path(directory).glob("*.tmp")))

    def test_intraday_checkpoint_requires_exact_identity_and_valid_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"checkpoint.json"; identity={"job_id":"job","engine_sha256":"abc"}
            path.write_text(json.dumps({"identity":identity,"checkpoint":{"format_version":1,"stability_rows":[],"completed_variants":0}}),encoding="utf-8")
            self.assertIsNotNone(_load_intraday_checkpoint(path,identity))
            self.assertIsNone(_load_intraday_checkpoint(path,{"job_id":"other"}))
            path.write_text("not-json",encoding="utf-8")
            self.assertIsNone(_load_intraday_checkpoint(path,identity))
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
                "replay_config": {
                    "warmup_bars": 61, "min_train_sessions": 1,
                    "test_sessions": 1, "require_benchmark": False,
                },
            }), encoding="utf-8")
            env = {
                "SERVICE_MODE": "historical_research",
                "BROKER_EXECUTION_ENABLED": "false",
                "GIT_COMMIT": "test-commit",
            }
            with patch.dict(os.environ, env, clear=False):
                output = execute_job(job, data, results)
            with zipfile.ZipFile(output) as bundle:
                manifest = json.loads(bundle.read("replay_manifest.json"))
            status = json.loads((results / "smoke-test.status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "complete")
            self.assertEqual(status["percent_complete"], 100.0)
            self.assertNotIn("failure_class", status)
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

    def test_worker_loads_checkpoint_from_continuation_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, results = root / "data", root / "results"
            data.mkdir(); results.mkdir()
            bars = data / "bars.csv"
            self._write_bars(bars)
            checkpoint = {
                "version": 1, "portfolios": {}, "history": {},
                "last_decision": None, "cycle_id": 41,
                "prior_close_equity": {},
            }
            with zipfile.ZipFile(results / "prior-results.zip", "w") as bundle:
                bundle.writestr("replay_checkpoint.json", json.dumps(checkpoint))
            job = root / "continued.json"
            job.write_text(json.dumps({
                "job_id": "continued", "bars_csv": "bars.csv",
                "continuation_of": "prior",
                "replay_config": {"warmup_bars": 61, "require_benchmark": False},
            }), encoding="utf-8")
            with patch.dict(os.environ, {
                "SERVICE_MODE": "historical_research",
                "BROKER_EXECUTION_ENABLED": "false",
            }, clear=False):
                output = execute_job(job, data, results)
            with zipfile.ZipFile(output) as bundle:
                written = json.loads(bundle.read("replay_checkpoint.json"))
            self.assertGreater(written["cycle_id"], 41)

    def test_retry_start_clears_stale_failure_and_progress_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, results = root / "data", root / "results"
            data.mkdir()
            results.mkdir()
            bars = data / "bars.csv"
            self._write_bars(bars)
            job = root / "retry.json"
            job.write_text(json.dumps({
                "job_id": "retry-test", "bars_csv": "bars.csv",
                "replay_config": {
                    "warmup_bars": 61, "min_train_sessions": 1,
                    "test_sessions": 1, "require_benchmark": False,
                },
            }), encoding="utf-8")
            (results / "retry-test.status.json").write_text(json.dumps({
                "job_id": "retry-test", "attempt_count": 2,
                "status": "failed", "error": "old failure",
                "failed_at": "2026-08-22T00:00:00+00:00",
                "percent_complete": 99.0, "completed_sessions": 600,
            }), encoding="utf-8")
            with patch.dict(os.environ, {
                "SERVICE_MODE": "historical_research",
                "BROKER_EXECUTION_ENABLED": "false",
            }, clear=False):
                execute_job(job, data, results)
            status = json.loads(
                (results / "retry-test.status.json").read_text(encoding="utf-8")
            )
            self.assertEqual("complete", status["status"])
            self.assertNotIn("error", status)
            self.assertNotIn("failed_at", status)
            self.assertNotEqual(600, status.get("completed_sessions"))

    def test_paths_cannot_escape_configured_root(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                _resolved_child(Path(directory), "../outside.csv")

    def test_durable_checkpoint_bundle_round_trips_spills_and_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spill = root / "cycles.jsonl.gz"
            import gzip
            with gzip.open(spill, "wt", encoding="utf-8") as handle:
                handle.write('{"cycle":1}\n')
            bundle = root / "checkpoint.zip"
            identity = {"job_id": "job", "source_sha256": "abc", "git_commit": "def"}
            _write_checkpoint_bundle(
                bundle, identity=identity,
                checkpoint={"completed_timestamps": 78},
                spills={"cycles": {"path": str(spill), "count": 1}},
            )
            checkpoint, spills = _restore_checkpoint_bundle(
                bundle, expected_identity=identity, spill_root=root / "restored",
            )
            self.assertEqual(78, checkpoint["completed_timestamps"])
            self.assertEqual(1, spills["cycles"]["count"])
            with gzip.open(spills["cycles"]["path"], "rt", encoding="utf-8") as handle:
                self.assertEqual('{"cycle":1}', handle.read().strip())
            rejected, rejected_spills = _restore_checkpoint_bundle(
                bundle, expected_identity={**identity, "source_sha256": "changed"},
                spill_root=root / "rejected",
            )
            self.assertIsNone(rejected)
            self.assertEqual({}, rejected_spills)

    def test_incremental_checkpoint_restores_concatenated_gzip_segments(self):
        class MemoryStore:
            def __init__(self):
                self.objects = {}
            def upload_file(self, path, key):
                self.objects[key] = Path(path).read_bytes()
            def download_file(self, key, path):
                if key not in self.objects:
                    return False
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_bytes(self.objects[key])
                return True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spill = root / "cycles.jsonl.gz"
            import gzip
            with gzip.open(spill, "wt", encoding="utf-8") as handle:
                handle.write('{"cycle":1}\n')
            identity = {"job_id": "job", "source_sha256": "abc", "engine_schema": 2}
            part_path = root / "part.zip"
            part0, offsets = _write_incremental_checkpoint_part(
                part_path, identity=identity,
                checkpoint={"completed_timestamps": 10},
                spills={"cycles": {"path": str(spill), "count": 1}},
                previous_offsets={}, sequence=0,
            )
            store = MemoryStore()
            store.upload_file(part_path, part0["key"])
            with gzip.open(spill, "at", encoding="utf-8") as handle:
                handle.write('{"cycle":2}\n')
            part1, offsets = _write_incremental_checkpoint_part(
                part_path, identity=identity,
                checkpoint={"completed_timestamps": 20},
                spills={"cycles": {"path": str(spill), "count": 2}},
                previous_offsets=offsets, sequence=1,
            )
            store.upload_file(part_path, part1["key"])
            chain = {
                "format_version": 2, "identity": identity,
                "parts": [part0, part1],
            }
            manifest = root / "manifest.json"
            _write_json_atomic(manifest, chain)
            store.upload_file(manifest, "checkpoints/job/manifest.json")
            checkpoint, spills, restored_chain, restored_offsets = (
                _restore_incremental_checkpoint(
                    store=store, job_id="job", expected_identity=identity,
                    results_root=root, spill_root=root / "restored",
                )
            )
            self.assertEqual(20, checkpoint["completed_timestamps"])
            self.assertEqual(2, spills["cycles"]["count"])
            self.assertEqual(2, len(restored_chain["parts"]))
            self.assertEqual(spill.stat().st_size, restored_offsets["cycles"])
            with gzip.open(spills["cycles"]["path"], "rt", encoding="utf-8") as handle:
                self.assertEqual([{"cycle": 1}, {"cycle": 2}], [json.loads(x) for x in handle])

    def test_incremental_checkpoint_rejects_missing_part(self):
        class MissingStore:
            def __init__(self, manifest):
                self.manifest = manifest
            def download_file(self, key, path):
                if key.endswith("manifest.json"):
                    Path(path).write_text(json.dumps(self.manifest), encoding="utf-8")
                    return True
                return False

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = {"job_id": "job", "source_sha256": "abc"}
            chain = {
                "format_version": 2, "identity": identity,
                "parts": [{
                    "sequence": 0, "key": "checkpoints/job/parts/000000.zip",
                    "sha256": "missing", "bytes": 1,
                }],
            }
            checkpoint, spills, restored_chain, offsets = _restore_incremental_checkpoint(
                store=MissingStore(chain), job_id="job", expected_identity=identity,
                results_root=root, spill_root=root / "restored",
            )
            self.assertIsNone(checkpoint)
            self.assertEqual({}, spills)
            self.assertIsNone(restored_chain)
            self.assertEqual({}, offsets)

    def test_known_compatible_engine_fingerprint_can_resume(self):
        from research.worker import _checkpoint_identity_matches
        stable = {
            "job_id": "job", "source_sha256": "source",
            "replay_config_sha256": "config",
        }
        self.assertTrue(_checkpoint_identity_matches({
            **stable,
            "engine_sha256": "ffbe90d7c90f8de10405925b8118f3ebc0d431ecff8754facac1a0994deda4be",
            "engine_schema": 2,
        }, {
            **stable, "engine_sha256": "new", "engine_schema": 2,
        }))


if __name__ == "__main__":
    unittest.main()
