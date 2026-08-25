"""Broker-free historical replay worker.

The worker polls a private job directory for JSON experiment requests. It never
imports the trading application, startup lifecycle, or Alpaca trading client.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import time
import traceback
import uuid
import zipfile
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

from config.service_mode import ServiceMode, validate_service_startup
from research.artifact_store import artifact_store_from_env
from research.historical_replay import ReplayConfig, SpilledRows, load_bar_csv, run_replay, write_replay
from research.universes import resolve_universe


UTC = timezone.utc


def _resource_snapshot() -> dict:
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        maximum_rss = float(usage.ru_maxrss)
        # Linux reports KiB; macOS reports bytes.
        maximum_rss_bytes = int(maximum_rss if sys.platform == "darwin" else maximum_rss * 1024)
    except ImportError:
        maximum_rss_bytes = 0
    return {"worker_max_rss_bytes": maximum_rss_bytes, "worker_pid": os.getpid()}


def _resolved_child(root: Path, value: str | Path) -> Path:
    root = root.resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path escapes configured root {root}: {candidate}")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _checkpoint_identity(job: dict, source_sha256: str, replay_config: ReplayConfig) -> dict:
    return {
        "job_id": str(job["job_id"]),
        "source_sha256": source_sha256,
        "replay_config_sha256": hashlib.sha256(
            json.dumps(
                {field.name: getattr(replay_config, field.name) for field in fields(ReplayConfig)},
                sort_keys=True, default=list,
            ).encode("utf-8")
        ).hexdigest(),
        "git_commit": os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT"),
    }


def _write_checkpoint_bundle(
    destination: Path, *, identity: dict, checkpoint: dict, spills: dict,
) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as bundle:
        manifest_spills = {}
        for name, state in spills.items():
            source = Path(state["path"])
            archive_name = f"spills/{name}.jsonl.gz"
            bundle.write(source, archive_name)
            manifest_spills[name] = {"archive_name": archive_name, "count": int(state["count"])}
        bundle.writestr("checkpoint.json", json.dumps({
            "format_version": 1, "identity": identity,
            "checkpoint": checkpoint, "spills": manifest_spills,
        }, separators=(",", ":"), default=str))
    temporary.replace(destination)


def _restore_checkpoint_bundle(
    bundle_path: Path, *, expected_identity: dict, spill_root: Path,
) -> tuple[dict | None, dict]:
    if not bundle_path.is_file():
        return None, {}
    try:
        with zipfile.ZipFile(bundle_path) as bundle:
            manifest = json.loads(bundle.read("checkpoint.json"))
            if manifest.get("identity") != expected_identity:
                return None, {}
            restored = {}
            spill_root.mkdir(parents=True, exist_ok=True)
            for name, state in dict(manifest.get("spills") or {}).items():
                destination = spill_root / f"{name}.restored.jsonl.gz"
                with bundle.open(state["archive_name"]) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                restored[name] = {"path": str(destination), "count": int(state["count"])}
            return dict(manifest["checkpoint"]), restored
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
        logging.warning("Ignoring invalid research checkpoint bundle %s", bundle_path, exc_info=True)
        return None, {}


def _config_from_job(job: dict) -> ReplayConfig:
    supplied = dict(job.get("replay_config") or {})
    allowed = {item.name for item in fields(ReplayConfig)}
    unknown = set(supplied) - allowed
    if unknown:
        raise ValueError(f"unknown replay_config fields: {sorted(unknown)}")
    if "candidate_symbols" in supplied:
        symbols = supplied["candidate_symbols"]
        if not isinstance(symbols, (list, tuple)):
            raise ValueError("candidate_symbols must be a JSON list")
        supplied["candidate_symbols"] = tuple(str(symbol) for symbol in symbols)
    return ReplayConfig(**supplied)


def execute_job(job_path: str | Path, data_root: str | Path, results_root: str | Path) -> Path:
    validate_service_startup(ServiceMode.HISTORICAL_RESEARCH)
    job_path = Path(job_path).resolve()
    job = json.loads(job_path.read_text(encoding="utf-8"))
    job_id = str(job.get("job_id") or "").strip()
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    if not job_id or any(character not in allowed for character in job_id):
        raise ValueError("job_id is required and may contain only letters, numbers, hyphens, and underscores")

    bars_path = _resolved_child(Path(data_root), job["bars_csv"])
    results_root = Path(results_root).resolve()
    output = _resolved_child(results_root, job_id)
    if not bars_path.is_file():
        raise FileNotFoundError(f"historical bars file does not exist: {bars_path}")
    if output.exists():
        raise FileExistsError(f"result directory already exists: {output}")

    started_at = datetime.now(UTC).isoformat()
    status_path = results_root / f"{job_id}.status.json"
    staging = results_root / f".{job_id}.{uuid.uuid4().hex}.tmp"
    spill = results_root / f".{job_id}.spill"
    if spill.exists():
        shutil.rmtree(spill)
    previous_status = {}
    if status_path.is_file():
        try:
            previous_status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    base_status = {
        **previous_status,
        "job_id": job_id, "status": "running", "started_at": started_at,
        "job_path": str(job_path), "bars_path": str(bars_path),
        "heartbeat_at": started_at, **_resource_snapshot(),
    }
    # A retry may reuse the durable status document from an earlier attempt.
    # Do not expose stale failure, completion, or progress metadata as if it
    # described the newly started worker.
    for stale_key in (
        "error", "error_type", "traceback", "failed_at", "failure_class",
        "retryable", "retries_exhausted", "retries_remaining",
        "next_retry_at", "completed_at", "archive", "durable_uri", "output",
        "completed_cycles", "completed_session", "completed_sessions",
        "completed_timestamps", "elapsed_seconds", "percent_complete",
        "stage", "stage_message", "stage_completed_rows",
        "stage_total_rows", "stage_percent_complete",
    ):
        base_status.pop(stale_key, None)
    _write_json_atomic(status_path, base_status)
    latest_status = dict(base_status)
    try:
        source_sha256 = _sha256_file(bars_path)

        def update_progress(progress: dict) -> None:
            latest_status.update(progress)
            latest_status.update({
                "heartbeat_at": datetime.now(UTC).isoformat(), **_resource_snapshot(),
            })
            _write_json_atomic(status_path, latest_status)

        replay_config = _config_from_job(job)
        store = artifact_store_from_env()
        checkpoint_key = f"checkpoints/{job_id}.zip"
        checkpoint_bundle = results_root / f".{job_id}.checkpoint.zip"
        identity = _checkpoint_identity(job, source_sha256, replay_config)
        if store.durable and not checkpoint_bundle.is_file():
            store.download_file(checkpoint_key, checkpoint_bundle)
        resumed_checkpoint, resumed_spills = _restore_checkpoint_bundle(
            checkpoint_bundle, expected_identity=identity, spill_root=spill,
        )
        initial_checkpoint = None
        continuation_of = str(job.get("continuation_of") or "").strip()
        if continuation_of:
            if any(character not in allowed for character in continuation_of):
                raise ValueError("continuation_of contains invalid characters")
            archive = results_root / f"{continuation_of}-results.zip"
            if not archive.is_file():
                raise FileNotFoundError(f"continuation archive is not available: {archive}")
            with zipfile.ZipFile(archive) as bundle:
                initial_checkpoint = json.loads(bundle.read("replay_checkpoint.json"))
            # Cross-job continuation carries investment and evaluation state,
            # but same-dataset cursor fields are only valid for restart recovery.
            for recovery_key in (
                "last_processed_timestamp", "completed_timestamps",
                "completed_session_dates",
            ):
                initial_checkpoint.pop(recovery_key, None)
            # The archive is a restored cache; R2 remains authoritative and
            # the checkpoint is now resident in memory.
            archive.unlink(missing_ok=True)

        if resumed_checkpoint:
            # A same-job durable checkpoint is newer than the prior-year
            # continuation state and already incorporates it.
            initial_checkpoint = resumed_checkpoint
            latest_status.update({
                "resumed_from_durable_checkpoint": True,
                "resumed_completed_session": (
                    list(resumed_checkpoint.get("completed_session_dates") or [None])[-1]
                ),
                "resumed_completed_timestamps": int(
                    resumed_checkpoint.get("completed_timestamps") or 0
                ),
            })
            _write_json_atomic(status_path, latest_status)

        checkpoint_upload_count = 0

        def persist_durable_checkpoint(checkpoint_payload: dict, spill_payload: dict) -> None:
            nonlocal checkpoint_upload_count
            _write_checkpoint_bundle(
                checkpoint_bundle, identity=identity,
                checkpoint=checkpoint_payload, spills=spill_payload,
            )
            if store.durable:
                durable_uri = store.upload_file(checkpoint_bundle, checkpoint_key)
            else:
                durable_uri = None
            checkpoint_upload_count += 1
            latest_status.update({
                "checkpoint_at": datetime.now(UTC).isoformat(),
                "checkpoint_completed_session": (
                    list(checkpoint_payload.get("completed_session_dates") or [None])[-1]
                ),
                "checkpoint_completed_timestamps": int(
                    checkpoint_payload.get("completed_timestamps") or 0
                ),
                "checkpoint_upload_count": checkpoint_upload_count,
                "checkpoint_durable_uri": durable_uri,
            })
            _write_json_atomic(status_path, latest_status)

        result = run_replay(
            load_bar_csv(
                bars_path,
                start_date=replay_config.data_start_date,
                end_date=replay_config.data_end_date,
                include_symbols=(
                    set(resolve_universe(replay_config.universe_name) or replay_config.candidate_symbols)
                    | {replay_config.benchmark_symbol}
                ) if (replay_config.universe_name or replay_config.candidate_symbols) else None,
            ), replay_config,
            progress_callback=update_progress, spill_directory=spill,
            initial_checkpoint=initial_checkpoint,
            initial_spills=resumed_spills,
            checkpoint_callback=persist_durable_checkpoint,
            checkpoint_every_sessions=max(
                1, int(os.getenv("RESEARCH_CHECKPOINT_EVERY_SESSIONS", "10"))
            ),
        )
        write_replay(
            result, staging, source_path=bars_path,
            source_sha256=source_sha256,
            experiment={"job_id": job_id, "job": job, "job_path": str(job_path)},
        )
        for value in result.values():
            if isinstance(value, SpilledRows):
                value.close()
        if spill.exists():
            shutil.rmtree(spill)
        staging.replace(output)
        completed_status = {
            **latest_status, "job_id": job_id, "status": "complete", "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(), "output": str(output),
            "percent_complete": 100.0,
        }
        for stale_key in (
            "error", "error_type", "traceback", "failed_at", "failure_class",
            "retryable", "retries_exhausted", "retries_remaining", "next_retry_at",
        ):
            completed_status.pop(stale_key, None)
        _write_json_atomic(status_path, completed_status)
        # The coordinator only removes this object after the final archive is
        # durably verified; until then it remains a restart recovery point.
        return output
    except Exception as exc:
        if spill.exists():
            shutil.rmtree(spill)
        if staging.exists():
            shutil.rmtree(staging)
        _write_json_atomic(status_path, {
            **latest_status, "job_id": job_id, "status": "failed", "started_at": started_at,
            "failed_at": datetime.now(UTC).isoformat(), "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        })
        raise


def run_worker(job_dir: Path, data_root: Path, results_root: Path, poll_seconds: float) -> None:
    validate_service_startup(ServiceMode.HISTORICAL_RESEARCH)
    job_dir.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)
    results_root.mkdir(parents=True, exist_ok=True)
    logging.info("Research worker ready; polling %s", job_dir)
    while True:
        jobs = sorted(job_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
        if not jobs:
            time.sleep(poll_seconds)
            continue
        job_path = jobs[0]
        # A unique claim name ensures that two workers racing on the same job
        # cannot rename or complete one another's claimed file.
        claimed = job_path.with_name(
            f"{job_path.name}.{os.getpid()}.{uuid.uuid4().hex}.running"
        )
        try:
            job_path.replace(claimed)
            execute_job(claimed, data_root, results_root)
            claimed.replace(claimed.with_name(f"{job_path.stem}.complete"))
        except Exception:
            logging.exception("Historical research job failed: %s", claimed)
            if claimed.exists():
                claimed.replace(claimed.with_name(f"{job_path.stem}.failed"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the broker-free historical research worker.")
    parser.add_argument("--job", help="Run one JSON job and exit instead of polling")
    parser.add_argument("--job-dir", default=os.getenv("RESEARCH_JOB_DIR", "research_jobs/inbox"))
    parser.add_argument("--data-root", default=os.getenv("RESEARCH_DATA_DIR", "research_data"))
    parser.add_argument("--results-root", default=os.getenv("RESEARCH_RESULTS_DIR", "research_results"))
    parser.add_argument("--poll-seconds", type=float, default=float(os.getenv("RESEARCH_POLL_SECONDS", "10")))
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    validate_service_startup(ServiceMode.HISTORICAL_RESEARCH)
    if hasattr(os, "nice"):
        try:
            os.nice(max(0, int(os.getenv("RESEARCH_WORKER_NICE", "15"))))
        except OSError:
            logging.warning("Could not lower research worker priority", exc_info=True)
    if args.poll_seconds < 1:
        parser.error("--poll-seconds must be at least 1")
    if args.job:
        output = execute_job(args.job, args.data_root, args.results_root)
        logging.info("Historical research job complete: %s", output)
        return 0
    run_worker(Path(args.job_dir), Path(args.data_root), Path(args.results_root), args.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
