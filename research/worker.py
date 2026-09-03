"""Broker-free historical replay worker.

The worker polls a private job directory for JSON experiment requests. It never
imports the trading application, startup lifecycle, or Alpaca trading client.
"""

from __future__ import annotations

import argparse
import hashlib
import io
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
from research.historical_replay import (
    ReplayConfig, SpilledRows, _drop_file_cache, _timestamp, load_bar_csv,
    run_replay, write_replay_archive,
)
from research.universes import resolve_universe
from research.strategy_registry import validate_experiment_declaration
from research.tier1_etf_replay import run_tier1_job


UTC = timezone.utc
CHECKPOINT_ENGINE_SCHEMA = 2
LEGACY_CHECKPOINT_COMMITS = {"61688bd5b95e414f788c721a6d994ea4c608916d"}
COMPATIBLE_CHECKPOINT_ENGINE_HASHES = {
    # 9b4dd8f: same state schema before adaptive checkpoint scheduling.
    "ffbe90d7c90f8de10405925b8118f3ebc0d431ecff8754facac1a0994deda4be",
    # e6ad700: deployed adaptive/progress-aware checkpoint format. The memory
    # changes preserve that schema so the currently running job can resume.
    "ef4acb8906cb097dfa5304a3b977ac004ed537a008e5f929540fdd3f487e6c98",
    # 42b2b76: memory-bounded checkpoint serialization; state schema unchanged.
    "22f68855cecc32589e5583c56d2216f737d55eeb266d3ebfe9927a1f80235378",
    # 24bed51: direct result archive finalization; state schema unchanged.
    "25a3241a992c971de15017ca411319ff8a55574c0c3235ec1f7be19c6bc6bd3d",
    # d0c2e9e: legacy tax-event compaction; state schema unchanged. Restoring
    # this checkpoint also compacts its still-expanded per-fill tax lots.
    "16053458cea91bf9c4c86feb32071ebaaa6de299cbbbebc16aa0de0417981ed6",
    # 72eacb2: restored-lot compaction and archive status throttling; state
    # schema unchanged while post-replay bar retention is reduced.
    "311baddd18fdc5dac6eabba68f4edf9d824e5954006bc16f49cf40ae76b6e26f",
    # 3e8cf35: source-row release before labels; checkpoint state unchanged.
    "5529cf0dc733f1da8ef04dfa76cb41128a7cfe868552eb0019a640a89b6cc117",
    # 6637c51: packed label arrays; checkpoint state schema unchanged.
    "d67819e740ead8420aea3ad918ebe179f0bad788f278dbdffbee160e64e5fe56",
    # 7f7e559: ZIP64 result members; checkpoint state schema unchanged.
    "794c2e5c073c11c4ff72a6a0f664fce1dc79bd007bcd8a60abc217d3a37dc88a",
    # 9a26b01: packed label timestamps and page-cache release; checkpoint
    # state schema unchanged.
    "445cd2ddca81c94c9b317c4ca36180ee115bc142664f2a74cba1a704a28a367c",
    # 6f9b30e: compact completed-checkpoint source bars; checkpoint state
    # schema unchanged.
    "00cafb6762cc7d0fef0681ea5917b2ca63d5676863f7e2fde87a46e98652fedd",
    # 4234e21: incremental gzip/ZIP cache eviction; checkpoint state schema
    # unchanged.
    "61f62a352ba6548fae6a2bc6f196255cc19f72d2b83ac96746de78f05d613f39",
}


def _resource_snapshot(storage_path: str | Path | None = None) -> dict:
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        maximum_rss = float(usage.ru_maxrss)
        # Linux reports KiB; macOS reports bytes.
        maximum_rss_bytes = int(maximum_rss if sys.platform == "darwin" else maximum_rss * 1024)
    except ImportError:
        maximum_rss_bytes = 0
    snapshot = {"worker_max_rss_bytes": maximum_rss_bytes, "worker_pid": os.getpid()}
    # Render uses cgroup accounting for the whole service. Worker RSS alone
    # misses the API/coordinator process and therefore understated the memory
    # pressure that triggers the 512 MiB instance limit.
    for key, candidates in {
        "service_memory_current_bytes": (
            "/sys/fs/cgroup/memory.current",
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
        ),
        "service_memory_limit_bytes": (
            "/sys/fs/cgroup/memory.max",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        ),
    }.items():
        for candidate in candidates:
            try:
                value = Path(candidate).read_text(encoding="utf-8").strip()
                if value != "max":
                    snapshot[key] = int(value)
                break
            except (OSError, ValueError):
                continue
    current = snapshot.get("service_memory_current_bytes")
    limit = snapshot.get("service_memory_limit_bytes")
    if current is not None and limit:
        snapshot["service_memory_pct"] = round(current / limit * 100.0, 2)
    if storage_path is not None:
        try:
            local_files = [
                path for path in Path(storage_path).rglob("*") if path.is_file()
            ]
            snapshot.update({
                "local_result_storage_bytes": sum(
                    path.stat().st_size for path in local_files
                ),
                "local_result_file_count": len(local_files),
            })
        except OSError:
            pass
    return snapshot


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
    engine_digest = hashlib.sha256()
    repository_root = Path(__file__).resolve().parents[1]
    for relative in (
        "research/historical_replay.py",
        "research/walk_forward.py",
        "research/universes.py",
        "layers/layer_research_strategy.py",
        "layers/layer1_ranker.py",
        "layers/layer2_portfolio.py",
        "layers/layer3_rebalancer.py",
    ):
        path = repository_root / relative
        engine_digest.update(relative.encode("utf-8"))
        engine_digest.update(path.read_bytes())
    return {
        "job_id": str(job["job_id"]),
        "source_sha256": source_sha256,
        "replay_config_sha256": hashlib.sha256(
            json.dumps(
                {field.name: getattr(replay_config, field.name) for field in fields(ReplayConfig)},
                sort_keys=True, default=list,
            ).encode("utf-8")
        ).hexdigest(),
        "engine_schema": CHECKPOINT_ENGINE_SCHEMA,
        "engine_sha256": engine_digest.hexdigest(),
    }


def _checkpoint_identity_matches(stored: dict, expected: dict) -> bool:
    if stored == expected:
        return True
    # One-time migration for checkpoints written by the first durable release.
    # It is deliberately restricted to that exact deployed commit and still
    # requires the job, dataset, and replay configuration to match.
    legacy_commit = str(stored.get("git_commit") or "")
    stable_keys = ("job_id", "source_sha256", "replay_config_sha256")
    return (
        (
            legacy_commit in LEGACY_CHECKPOINT_COMMITS
            or stored.get("engine_sha256") in COMPATIBLE_CHECKPOINT_ENGINE_HASHES
        )
        and all(stored.get(key) == expected.get(key) for key in stable_keys)
    )


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
            _drop_file_cache(source)
            manifest_spills[name] = {"archive_name": archive_name, "count": int(state["count"])}
        # Stream the manifest into the archive. json.dumps temporarily held a
        # second complete checkpoint string in memory and was the dominant
        # near-completion allocation spike on the free Render instance.
        with bundle.open("checkpoint.json", "w") as raw_handle:
            with io.TextIOWrapper(raw_handle, encoding="utf-8") as text_handle:
                json.dump({
                    "format_version": 1, "identity": identity,
                    "checkpoint": checkpoint, "spills": manifest_spills,
                }, text_handle, separators=(",", ":"), default=str)
    temporary.replace(destination)


def _restore_checkpoint_bundle(
    bundle_path: Path, *, expected_identity: dict, spill_root: Path,
) -> tuple[dict | None, dict]:
    if not bundle_path.is_file():
        return None, {}
    try:
        with zipfile.ZipFile(bundle_path) as bundle:
            manifest = json.loads(bundle.read("checkpoint.json"))
            if not _checkpoint_identity_matches(
                dict(manifest.get("identity") or {}), expected_identity
            ):
                return None, {}
            restored = {}
            spill_root.mkdir(parents=True, exist_ok=True)
            for name, state in dict(manifest.get("spills") or {}).items():
                destination = spill_root / f"{name}.restored.jsonl.gz"
                with bundle.open(state["archive_name"]) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                _drop_file_cache(destination)
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
    final_archive = _resolved_child(results_root, f"{job_id}-results.zip")
    if not bars_path.is_file():
        raise FileNotFoundError(f"historical bars file does not exist: {bars_path}")
    if output.exists() or final_archive.exists():
        raise FileExistsError(f"result artifact already exists for job: {job_id}")

    started_at = datetime.now(UTC).isoformat()
    status_path = results_root / f"{job_id}.status.json"
    staging = results_root / f".{job_id}.{uuid.uuid4().hex}.results.zip.tmp"
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
        "source_dataset_bytes": bars_path.stat().st_size,
        "heartbeat_at": started_at, **_resource_snapshot(results_root),
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
                "heartbeat_at": datetime.now(UTC).isoformat(),
                **_resource_snapshot(results_root),
            })
            _write_json_atomic(status_path, latest_status)

        engine = str(job.get("engine") or "intraday_replay").strip().lower()
        if engine == "tier1_etf_daily":
            update_progress({"stage": "loading_tier1_daily_data"})
            run_tier1_job(
                job, bars_path, staging, source_sha256,
                progress_callback=update_progress,
            )
            update_progress({
                "stage": "result_archive_ready",
                "archive_bytes_written": staging.stat().st_size,
                "archive_percent_complete": 100.0,
            })
            staging.replace(final_archive)
            completed_status = {
                **latest_status, "job_id": job_id, "status": "complete",
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "archive": str(final_archive), "percent_complete": 100.0,
                "research_engine": engine,
            }
            for stale_key in (
                "error", "error_type", "traceback", "failed_at",
                "failure_class", "retryable", "retries_exhausted",
                "retries_remaining", "next_retry_at",
            ):
                completed_status.pop(stale_key, None)
            _write_json_atomic(status_path, completed_status)
            return final_archive
        if engine != "intraday_replay":
            raise ValueError(f"unknown research engine: {engine}")

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
        if store.durable:
            # The durable copy is authoritative. Keeping the downloaded bundle
            # beside its extracted spills needlessly duplicates hundreds of
            # megabytes on Render's 2 GiB temporary volume.
            checkpoint_bundle.unlink(missing_ok=True)
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
            restored_timestamps = int(
                resumed_checkpoint.get("completed_timestamps") or 0
            )
            restored_session = (
                list(resumed_checkpoint.get("completed_session_dates") or [None])[-1]
            )
            latest_status.update({
                "resumed_from_durable_checkpoint": True,
                "resumed_completed_session": restored_session,
                "resumed_completed_timestamps": restored_timestamps,
                # The recovery bundle is authoritative even when the periodic
                # durable status snapshot lagged behind it during a restart.
                "checkpoint_completed_session": restored_session,
                "checkpoint_completed_timestamps": restored_timestamps,
                "attempt_started_checkpoint_timestamps": restored_timestamps,
                "checkpoint_restored_at": datetime.now(UTC).isoformat(),
            })
            _write_json_atomic(status_path, latest_status)

        checkpoint_upload_count = 0
        checkpoint_egress_bytes = int(latest_status.get("checkpoint_egress_bytes") or 0)
        checkpoint_egress_budget = max(
            0, int(os.getenv("RESEARCH_CHECKPOINT_EGRESS_BUDGET_BYTES", str(1024**3)))
        )

        def persist_durable_checkpoint(checkpoint_payload: dict, spill_payload: dict) -> None:
            nonlocal checkpoint_upload_count, checkpoint_egress_bytes
            _write_checkpoint_bundle(
                checkpoint_bundle, identity=identity,
                checkpoint=checkpoint_payload, spills=spill_payload,
            )
            checkpoint_size = checkpoint_bundle.stat().st_size
            upload_allowed = checkpoint_egress_bytes + checkpoint_size <= checkpoint_egress_budget
            if store.durable and upload_allowed:
                durable_uri = store.upload_file(checkpoint_bundle, checkpoint_key)
                _drop_file_cache(checkpoint_bundle)
                checkpoint_egress_bytes += checkpoint_size
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
                "checkpoint_egress_bytes": checkpoint_egress_bytes,
                "checkpoint_egress_budget_bytes": checkpoint_egress_budget,
                "checkpoint_upload_skipped_egress_budget": bool(
                    store.durable and not upload_allowed
                ),
                "checkpoint_durable_uri": durable_uri,
            })
            _write_json_atomic(status_path, latest_status)
            if store.durable:
                checkpoint_bundle.unlink(missing_ok=True)

        included_symbols = (
            set(resolve_universe(replay_config.universe_name) or replay_config.candidate_symbols)
            | {replay_config.benchmark_symbol}
        ) if (replay_config.universe_name or replay_config.candidate_symbols) else None
        completed_through = (
            _timestamp(resumed_checkpoint["last_processed_timestamp"])
            if resumed_checkpoint and resumed_checkpoint.get("last_processed_timestamp")
            else None
        )
        replay_rows = load_bar_csv(
            bars_path,
            start_date=replay_config.data_start_date,
            end_date=replay_config.data_end_date,
            include_symbols=included_symbols,
            compact_for_postprocess=completed_through is not None,
            compact_for_replay=completed_through is None,
        )
        # Compact rows omit volume/trade/vwap and are valid only when the
        # durable checkpoint covers every selected bar. Fall back safely if a
        # partial checkpoint is ever passed through this path.
        if completed_through and replay_rows and replay_rows[-1]["timestamp"] > completed_through:
            replay_rows.clear()
            replay_rows = load_bar_csv(
                bars_path,
                start_date=replay_config.data_start_date,
                end_date=replay_config.data_end_date,
                include_symbols=included_symbols,
                compact_for_replay=True,
            )
        result = run_replay(
            replay_rows, replay_config,
            progress_callback=update_progress, spill_directory=spill,
            initial_checkpoint=initial_checkpoint,
            initial_spills=resumed_spills,
            checkpoint_callback=persist_durable_checkpoint,
            checkpoint_every_sessions=max(
                1, int(os.getenv("RESEARCH_CHECKPOINT_EVERY_SESSIONS", "100"))
            ),
            release_source_rows=True,
        )
        update_progress({"stage": "writing_result_archive"})
        last_archive_progress_at = 0.0

        def update_archive_progress(progress: dict) -> None:
            nonlocal last_archive_progress_at
            now = time.monotonic()
            if now - last_archive_progress_at < 5.0:
                return
            last_archive_progress_at = now
            update_progress({"stage": "writing_result_archive", **progress})

        write_replay_archive(
            result, staging, source_path=bars_path,
            source_sha256=source_sha256,
            experiment={
                "job_id": job_id,
                "declaration": validate_experiment_declaration(job.get("experiment")),
                "job": job,
                "job_path": str(job_path),
            },
            release_spills=True,
            progress_callback=update_archive_progress,
        )
        update_progress({
            "stage": "result_archive_ready",
            "archive_bytes_written": staging.stat().st_size,
            "archive_percent_complete": 100.0,
        })
        for value in result.values():
            if isinstance(value, SpilledRows):
                value.close()
        if spill.exists():
            shutil.rmtree(spill)
        staging.replace(final_archive)
        completed_status = {
            **latest_status, "job_id": job_id, "status": "complete", "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "archive": str(final_archive),
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
        return final_archive
    except Exception as exc:
        if spill.exists():
            shutil.rmtree(spill)
        if staging.exists():
            staging.unlink(missing_ok=True)
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
