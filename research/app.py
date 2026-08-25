"""Authenticated, broker-free web coordinator for historical research jobs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse

from config.service_mode import ServiceMode, validate_service_startup
from research.artifact_store import artifact_store_from_env
from research.job_queue import classify_failure, queued_in_fifo_order, retry_delay_seconds
from research.worker import _write_json_atomic


class StorageBudgetExceeded(RuntimeError):
    pass


class ResearchRuntime:
    def __init__(self) -> None:
        self.data_root = Path(os.getenv("RESEARCH_DATA_DIR", "/tmp/research/data")).resolve()
        self.job_root = Path(os.getenv("RESEARCH_JOB_DIR", "/tmp/research/jobs")).resolve()
        self.results_root = Path(os.getenv("RESEARCH_RESULTS_DIR", "/tmp/research/results")).resolve()
        self.maximum_upload_bytes = int(os.getenv("RESEARCH_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
        self.api_token = str(os.getenv("RESEARCH_API_TOKEN") or "")
        self.active_job_id: str | None = None
        self.lock = asyncio.Lock()
        self.queue_lock = threading.RLock()
        self.store = None
        self.maximum_queued_jobs = 20
        self.status_upload_seconds = 60.0
        self.storage_budget_bytes = 8 * 1024**3
        self.maximum_retries = 3
        self.retry_base_seconds = 30.0
        self.retry_max_seconds = 300.0
        self.cleanup_local_artifacts = True
        self._storage_usage_cache: tuple[float, int] | None = None

    def initialize(self) -> None:
        validate_service_startup(ServiceMode.HISTORICAL_RESEARCH)
        self.data_root = Path(os.getenv("RESEARCH_DATA_DIR", "/tmp/research/data")).resolve()
        self.job_root = Path(os.getenv("RESEARCH_JOB_DIR", "/tmp/research/jobs")).resolve()
        self.results_root = Path(os.getenv("RESEARCH_RESULTS_DIR", "/tmp/research/results")).resolve()
        self.maximum_upload_bytes = int(os.getenv("RESEARCH_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
        self.api_token = str(os.getenv("RESEARCH_API_TOKEN") or "")
        self.maximum_queued_jobs = max(1, int(os.getenv("RESEARCH_MAX_QUEUED_JOBS", "20")))
        self.status_upload_seconds = max(30.0, float(os.getenv("RESEARCH_STATUS_UPLOAD_SECONDS", "60")))
        self.storage_budget_bytes = max(
            1024**3, int(os.getenv("RESEARCH_R2_STORAGE_BUDGET_BYTES", str(8 * 1024**3)))
        )
        self.maximum_retries = max(0, int(os.getenv("RESEARCH_MAX_RETRIES", "3")))
        self.retry_base_seconds = max(5.0, float(os.getenv("RESEARCH_RETRY_BASE_SECONDS", "30")))
        self.retry_max_seconds = max(
            self.retry_base_seconds, float(os.getenv("RESEARCH_RETRY_MAX_SECONDS", "300"))
        )
        self.cleanup_local_artifacts = str(
            os.getenv("RESEARCH_CLEANUP_LOCAL_ARTIFACTS", "true")
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._storage_usage_cache = None
        if len(self.api_token) < 24:
            raise RuntimeError("RESEARCH_API_TOKEN must contain at least 24 characters")
        for path in (self.data_root, self.job_root, self.results_root):
            path.mkdir(parents=True, exist_ok=True)
        self.store = artifact_store_from_env()

    def ensure_storage_capacity(self, path: Path) -> None:
        if not self.store.durable:
            return
        now = time.monotonic()
        if not self._storage_usage_cache or now - self._storage_usage_cache[0] >= 300:
            self._storage_usage_cache = (now, self.store.total_bytes())
        used = self._storage_usage_cache[1]
        if used + path.stat().st_size > self.storage_budget_bytes:
            raise StorageBudgetExceeded(
                f"R2 safety budget would be exceeded: {used + path.stat().st_size} "
                f"> {self.storage_budget_bytes} bytes"
            )

    def record_storage_write(self, size: int) -> None:
        if self._storage_usage_cache:
            checked_at, used = self._storage_usage_cache
            self._storage_usage_cache = (checked_at, used + max(0, int(size)))


runtime = ResearchRuntime()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    runtime.initialize()
    _restore_durable_state()
    _recover_interrupted_job()
    yield


app = FastAPI(title="Stock Trader Historical Research", lifespan=lifespan)


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {runtime.api_token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid research API token")


def _safe_name(value: str, suffix: str | None = None) -> str:
    allowed = (
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_ ."
        if suffix else
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    )
    if not value or any(character not in allowed for character in value) or Path(value).name != value:
        raise HTTPException(status_code=400, detail="invalid name")
    if suffix and not value.lower().endswith(suffix):
        raise HTTPException(status_code=400, detail=f"name must end with {suffix}")
    return value


def _status(job_id: str) -> dict:
    path = runtime.results_root / f"{job_id}.status.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="job not found")
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _upload_status_snapshot(
    status_path: Path,
    *,
    durable_key: str,
    previous_digest: str | None = None,
) -> str | None:
    """Upload an immutable status snapshot without disrupting job execution."""
    snapshot = status_path.with_name(
        f".{status_path.name}.{uuid.uuid4().hex}.uploading"
    )
    try:
        payload = status_path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest == previous_digest:
            return previous_digest
        snapshot.write_bytes(payload)
        runtime.store.upload_file(snapshot, durable_key)
        return digest
    except Exception:
        logging.warning(
            "Could not upload research status snapshot: %s",
            status_path.name,
            exc_info=True,
        )
        return previous_digest
    finally:
        snapshot.unlink(missing_ok=True)


@app.api_route("/healthz", methods=["GET", "HEAD"])
@app.api_route("/api/public/uptime-health", methods=["GET", "HEAD"])
def health() -> dict:
    return {
        "status": "ok",
        "service_mode": "historical_research",
        "git_commit": os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT"),
        "git_branch": os.getenv("RENDER_GIT_BRANCH") or os.getenv("GIT_BRANCH"),
        "busy": runtime.active_job_id is not None,
        "active_job_id": runtime.active_job_id,
        "queued_jobs": _queued_job_count(),
        "durable_storage_configured": bool(runtime.store and runtime.store.durable),
        "storage_budget_bytes": runtime.storage_budget_bytes,
    }


@app.put("/api/datasets/{filename}", dependencies=[Depends(require_token)])
async def upload_dataset(filename: str, request: Request) -> dict:
    filename = _safe_name(filename, ".csv")
    destination = runtime.data_root / filename
    temporary = destination.with_suffix(destination.suffix + ".uploading")
    size = 0
    digest = hashlib.sha256()
    try:
        with temporary.open("wb") as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > runtime.maximum_upload_bytes:
                    raise HTTPException(status_code=413, detail="dataset exceeds configured upload limit")
                digest.update(chunk)
                handle.write(chunk)
        if destination.exists():
            existing_digest = _sha256_file(destination)
            if existing_digest != digest.hexdigest():
                raise HTTPException(
                    status_code=409,
                    detail="a different dataset already uses this filename",
                )
            temporary.unlink()
        else:
            temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    try:
        runtime.ensure_storage_capacity(destination)
    except RuntimeError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    durable_uri = runtime.store.upload_file(destination, f"datasets/{digest.hexdigest()}-{filename}")
    runtime.record_storage_write(destination.stat().st_size)
    return {"filename": filename, "bytes": size, "sha256": digest.hexdigest(), "durable_uri": durable_uri}


def _run_job(job_id: str, job_path: Path) -> None:
    succeeded = False
    retry_delay = None
    try:
        output = runtime.results_root / job_id
        status_path = runtime.results_root / f"{job_id}.status.json"
        if not (output / "replay_manifest.json").is_file():
            command = [
                sys.executable, "-m", "research.worker", "--job", str(job_path),
                "--data-root", str(runtime.data_root), "--results-root", str(runtime.results_root),
            ]
            process = subprocess.Popen(command, cwd=str(Path(__file__).resolve().parents[1]))
            last_uploaded_digest = None
            while True:
                if status_path.is_file() and runtime.store.durable:
                    last_uploaded_digest = _upload_status_snapshot(
                        status_path,
                        durable_key=f"status/{status_path.name}",
                        previous_digest=last_uploaded_digest,
                    )
                try:
                    process.wait(timeout=runtime.status_upload_seconds)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                if process.returncode < 0:
                    raise RuntimeError(
                        f"research worker terminated by signal {-process.returncode}; "
                        "the service may have exceeded its memory or CPU allowance"
                    )
                raise RuntimeError(f"research worker exited with code {process.returncode}")
        if not output.is_dir():
            raise RuntimeError("research worker completed without a result directory")
        archive_base = runtime.results_root / f"{job_id}-results"
        archive = Path(shutil.make_archive(str(archive_base), "zip", root_dir=output))
        runtime.ensure_storage_capacity(archive)
        result_uri = runtime.store.upload_file(archive, f"results/{archive.name}")
        runtime.record_storage_write(archive.stat().st_size)
        status_payload = _status(job_id)
        status_payload.update({
            "status": "complete", "archive": str(archive), "durable_uri": result_uri,
            "completed_at": status_payload.get("completed_at") or datetime.now(timezone.utc).isoformat(),
        })
        _write_json_atomic(runtime.results_root / f"{job_id}.status.json", status_payload)
        succeeded = True
    except Exception as exc:
        logging.exception("Historical research job failed: %s", job_id)
        status_path = runtime.results_root / f"{job_id}.status.json"
        previous = {}
        if status_path.is_file():
            try:
                previous = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        attempt_count = max(1, int(previous.get("attempt_count") or 1))
        retryable, failure_class = _classify_failure(exc, previous)
        if retryable and attempt_count <= runtime.maximum_retries:
            retry_delay = retry_delay_seconds(
                attempt_count, maximum_retries=runtime.maximum_retries,
                base_seconds=runtime.retry_base_seconds,
                maximum_seconds=runtime.retry_max_seconds,
            )
            next_retry = datetime.now(timezone.utc) + timedelta(seconds=retry_delay)
            _write_json_atomic(status_path, {
                **previous, "job_id": job_id, "status": "retry_wait",
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc), "failure_class": failure_class,
                "retryable": True, "next_retry_at": next_retry.isoformat(),
                "retries_remaining": runtime.maximum_retries - attempt_count + 1,
                "blocks_queue": False,
            })
        else:
            _write_json_atomic(status_path, {
                **previous, "job_id": job_id, "status": "failed",
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc), "failure_class": failure_class,
                "retryable": retryable, "retries_exhausted": retryable,
                "retries_remaining": 0, "next_retry_at": None, "blocks_queue": True,
            })
    finally:
        status_path = runtime.results_root / f"{job_id}.status.json"
        if status_path.is_file():
            _upload_status_snapshot(
                status_path,
                durable_key=f"status/{status_path.name}",
            )
        if succeeded and runtime.store.durable:
            # The final result archive and complete status are now durable, so
            # the rolling recovery bundle is no longer needed or billable.
            runtime.store.delete_file(f"checkpoints/{job_id}.zip")
        if runtime.store.durable and runtime.cleanup_local_artifacts:
            _cleanup_local_job_artifacts(job_id, job_path)
        with runtime.queue_lock:
            runtime.active_job_id = None
        if succeeded:
            _start_next_job()
        elif retry_delay is not None:
            threading.Timer(retry_delay, _start_next_job).start()


def _classify_failure(exc: Exception, status_payload: dict) -> tuple[bool, str]:
    error_type = str(status_payload.get("error_type") or type(exc).__name__)
    return classify_failure(
        error_type, storage_budget_exceeded=isinstance(exc, StorageBudgetExceeded),
    )


def _restore_durable_state() -> None:
    if not runtime.store.durable:
        return
    for key in runtime.store.list_keys("status/"):
        if key.endswith(".status.json"):
            runtime.store.download_file(key, runtime.results_root / Path(key).name)
    for key in runtime.store.list_keys("jobs/"):
        if key.endswith(".json"):
            runtime.store.download_file(key, runtime.job_root / Path(key).name)


def _restore_dataset(filename: str) -> bool:
    destination = runtime.data_root / filename
    if destination.is_file():
        return True
    if not runtime.store.durable:
        return False
    matches = [key for key in runtime.store.list_keys("datasets/") if key.endswith("-" + filename)]
    return bool(matches and runtime.store.download_file(matches[-1], destination))


def _cleanup_local_job_artifacts(job_id: str, job_path: Path) -> None:
    """Keep Render's ephemeral filesystem as a cache; R2 remains authoritative."""
    try:
        job = json.loads(job_path.read_text(encoding="utf-8")) if job_path.is_file() else {}
        bars_csv = str(job.get("bars_csv") or "")
        if bars_csv:
            dataset = runtime.data_root / Path(bars_csv).name
            if dataset.is_file():
                dataset.unlink()
        output = runtime.results_root / job_id
        if output.is_dir():
            shutil.rmtree(output)
        archive = runtime.results_root / f"{job_id}-results.zip"
        if archive.is_file():
            archive.unlink()
        for path in runtime.results_root.glob(f".{job_id}.*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file():
                path.unlink(missing_ok=True)
    except Exception:
        logging.exception("Could not clean local research artifacts for %s", job_id)


def _queued_statuses() -> list[dict]:
    queued = []
    for path in runtime.results_root.glob("*.status.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("status") == "queued":
            queued.append(payload)
    return queued_in_fifo_order(queued)


def _queued_job_count() -> int:
    return len(_queued_statuses())


def _launch_job(job_id: str, job_path: Path) -> bool:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    if not _restore_dataset(str(job.get("bars_csv") or "")):
        return False
    continuation_of = str(job.get("continuation_of") or "").strip()
    if continuation_of:
        archive = runtime.results_root / f"{continuation_of}-results.zip"
        if not archive.is_file() and runtime.store.durable:
            runtime.store.download_file(f"results/{archive.name}", archive)
        if not archive.is_file():
            return False
    status_path = runtime.results_root / f"{job_id}.status.json"
    previous = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {}
    _write_json_atomic(status_path, {
        **previous, "job_id": job_id, "status": "starting",
        "started_by_coordinator_at": datetime.now(timezone.utc).isoformat(),
        "attempt_count": int(previous.get("attempt_count") or 0) + 1,
        "next_retry_at": None, "blocks_queue": False,
    })
    runtime.active_job_id = job_id
    threading.Thread(target=_run_job, args=(job_id, job_path), daemon=True).start()
    return True


def _start_next_job() -> None:
    with runtime.queue_lock:
        if runtime.active_job_id:
            return
        for path in runtime.results_root.glob("*.status.json"):
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("blocks_queue"):
                    return
            except (OSError, json.JSONDecodeError):
                continue
        retrying = []
        now = datetime.now(timezone.utc)
        for path in runtime.results_root.glob("*.status.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("status") != "retry_wait":
                continue
            retry_at = datetime.fromisoformat(str(payload["next_retry_at"]).replace("Z", "+00:00"))
            retrying.append((retry_at, payload))
        if retrying:
            retry_at, payload = min(retrying, key=lambda item: item[0])
            delay = max(0.0, (retry_at - now).total_seconds())
            if delay > 0:
                threading.Timer(delay, _start_next_job).start()
                return
            job_id = str(payload["job_id"])
            job_path = runtime.job_root / f"{job_id}.json"
            if job_path.is_file() and _launch_job(job_id, job_path):
                return
        for payload in _queued_statuses():
            job_id = str(payload.get("job_id") or "")
            job_path = runtime.job_root / f"{job_id}.json"
            if not job_path.is_file():
                status_path = runtime.results_root / f"{job_id}.status.json"
                _write_json_atomic(status_path, {
                    **payload, "status": "failed", "blocks_queue": True,
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "error": "durable job definition is missing",
                })
                return
            if _launch_job(job_id, job_path):
                return
            status_path = runtime.results_root / f"{job_id}.status.json"
            _write_json_atomic(status_path, {
                **payload, "status": "failed", "blocks_queue": True,
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "error": "referenced dataset could not be restored",
            })
            runtime.store.upload_file(status_path, f"status/{status_path.name}")
            return


def _recover_interrupted_job() -> None:
    candidates = []
    for path in runtime.results_root.glob("*.status.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") in {"running", "starting"}:
            candidates.append(payload)
    if not candidates:
        _start_next_job()
        return
    payload = max(candidates, key=lambda item: str(item.get("started_at") or ""))
    job_id = str(payload["job_id"])
    job_path = runtime.job_root / f"{job_id}.json"
    if not job_path.is_file():
        return
    attempt_count = max(1, int(payload.get("attempt_count") or 1))
    status_path = runtime.results_root / f"{job_id}.status.json"
    if attempt_count > runtime.maximum_retries:
        _write_json_atomic(status_path, {
            **payload, "status": "failed", "blocks_queue": True,
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "failure_class": "interrupted_service_restart",
            "error": "automatic restart retry allowance exhausted",
            "retryable": True, "retries_exhausted": True,
            "retries_remaining": 0, "next_retry_at": None,
        })
        runtime.store.upload_file(status_path, f"status/{status_path.name}")
        return
    delay = retry_delay_seconds(
        attempt_count, maximum_retries=runtime.maximum_retries,
        base_seconds=runtime.retry_base_seconds,
        maximum_seconds=runtime.retry_max_seconds,
    )
    _write_json_atomic(status_path, {
        **payload, "status": "retry_wait", "blocks_queue": False,
        "failure_class": "interrupted_service_restart", "retryable": True,
        "next_retry_at": (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(),
        "retries_remaining": runtime.maximum_retries - attempt_count + 1,
    })
    runtime.store.upload_file(status_path, f"status/{status_path.name}")
    _start_next_job()


@app.post("/api/jobs", status_code=202, dependencies=[Depends(require_token)])
async def submit_job(job: dict) -> dict:
    job_id = _safe_name(str(job.get("job_id") or ""))
    bars_csv = _safe_name(str(job.get("bars_csv") or ""), ".csv")
    job = {**job, "job_id": job_id, "bars_csv": bars_csv}
    if not _restore_dataset(bars_csv):
        raise HTTPException(status_code=400, detail="dataset has not been uploaded")
    async with runtime.lock:
        if _queued_job_count() >= runtime.maximum_queued_jobs:
            raise HTTPException(status_code=429, detail="research queue is full")
        if (runtime.results_root / job_id).exists() or (runtime.job_root / f"{job_id}.json").exists():
            raise HTTPException(status_code=409, detail="job id already exists")
        job_path = runtime.job_root / f"{job_id}.json"
        _write_json_atomic(job_path, job)
        job_uri = runtime.store.upload_file(job_path, f"jobs/{job_path.name}")
        queued_at = datetime.now(timezone.utc).isoformat()
        status_path = runtime.results_root / f"{job_id}.status.json"
        _write_json_atomic(status_path, {
            "job_id": job_id, "status": "queued", "queued_at": queued_at,
            "bars_csv": bars_csv,
        })
        runtime.store.upload_file(status_path, f"status/{status_path.name}")
        _start_next_job()
        queued_ids = [item["job_id"] for item in _queued_statuses()]
        queue_position = 0 if runtime.active_job_id == job_id else queued_ids.index(job_id) + 1
    return {
        "job_id": job_id, "status": "starting" if queue_position == 0 else "queued",
        "queue_position": queue_position, "durable_job_uri": job_uri,
    }


@app.post("/api/queue/resume", dependencies=[Depends(require_token)])
def resume_queue() -> dict:
    cleared = []
    with runtime.queue_lock:
        for path in runtime.results_root.glob("*.status.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not payload.pop("blocks_queue", None):
                continue
            payload["queue_block_cleared_at"] = datetime.now(timezone.utc).isoformat()
            _write_json_atomic(path, payload)
            runtime.store.upload_file(path, f"status/{path.name}")
            cleared.append(str(payload.get("job_id") or path.stem))
        _start_next_job()
    return {"status": "resumed", "cleared_failures": cleared, "active_job_id": runtime.active_job_id}


@app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(require_token)])
def retry_failed_job(job_id: str) -> dict:
    """Requeue a failed job under the same ID so its checkpoint remains addressable."""
    job_id = _safe_name(job_id)
    status_path = runtime.results_root / f"{job_id}.status.json"
    job_path = runtime.job_root / f"{job_id}.json"
    with runtime.queue_lock:
        if runtime.active_job_id == job_id:
            raise HTTPException(status_code=409, detail="job is already running")
        if not status_path.is_file():
            raise HTTPException(status_code=404, detail="job status is not available")
        if not job_path.is_file() and runtime.store.durable:
            runtime.store.download_file(f"jobs/{job_path.name}", job_path)
        if not job_path.is_file():
            raise HTTPException(status_code=409, detail="durable job definition is missing")
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        if payload.get("status") != "failed":
            raise HTTPException(status_code=409, detail="only a failed job can be retried")
        prior_attempts = int(payload.get("attempt_count") or 0)
        retried_at = datetime.now(timezone.utc).isoformat()
        last_failure = {
            key: payload.get(key) for key in (
                "failed_at", "error", "error_type", "failure_class",
                "retries_exhausted",
            ) if payload.get(key) is not None
        }
        payload.update({
            "status": "queued", "attempt_count": 0,
            "lifetime_attempt_count": int(payload.get("lifetime_attempt_count") or 0) + prior_attempts,
            "manual_retry_count": int(payload.get("manual_retry_count") or 0) + 1,
            "manual_retry_at": retried_at, "last_failure": last_failure,
            "blocks_queue": False, "next_retry_at": None,
            "retries_remaining": runtime.maximum_retries,
            "retries_exhausted": False,
        })
        for stale_key in ("error", "error_type", "failure_class", "failed_at"):
            payload.pop(stale_key, None)
        _write_json_atomic(status_path, payload)
        runtime.store.upload_file(status_path, f"status/{status_path.name}")
        _start_next_job()
    return {
        "job_id": job_id,
        "status": "starting" if runtime.active_job_id == job_id else "queued",
        "active_job_id": runtime.active_job_id,
        "manual_retry_count": payload["manual_retry_count"],
        "lifetime_attempt_count": payload["lifetime_attempt_count"],
    }


@app.post("/api/jobs/{job_id}/supersede", dependencies=[Depends(require_token)])
def supersede_job(job_id: str) -> dict:
    """Retire obsolete queued/retry work without deleting its audit history."""
    job_id = _safe_name(job_id)
    status_path = runtime.results_root / f"{job_id}.status.json"
    with runtime.queue_lock:
        if runtime.active_job_id == job_id:
            raise HTTPException(
                status_code=409,
                detail="a running job cannot be superseded; wait for it to stop or restart the service",
            )
        if not status_path.is_file():
            raise HTTPException(status_code=404, detail="job status is not available")
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        if payload.get("status") == "complete":
            raise HTTPException(status_code=409, detail="a completed job cannot be superseded")
        payload.update({
            "status": "superseded",
            "blocks_queue": False,
            "next_retry_at": None,
            "superseded_at": datetime.now(timezone.utc).isoformat(),
        })
        _write_json_atomic(status_path, payload)
        runtime.store.upload_file(status_path, f"status/{status_path.name}")
        _start_next_job()
    return {"job_id": job_id, "status": "superseded", "active_job_id": runtime.active_job_id}


@app.get("/api/jobs", dependencies=[Depends(require_token)])
def list_jobs() -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(runtime.results_root.glob("*.status.json"), reverse=True)
    ]


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_token)])
def job_status(job_id: str) -> dict:
    return _status(_safe_name(job_id))


@app.get("/api/jobs/{job_id}/download", dependencies=[Depends(require_token)])
def download_results(job_id: str):
    job_id = _safe_name(job_id)
    archive = runtime.results_root / f"{job_id}-results.zip"
    if not archive.is_file() and runtime.store.durable:
        runtime.store.download_file(f"results/{archive.name}", archive)
    if not archive.is_file():
        raise HTTPException(status_code=404, detail="result archive is not available")
    return FileResponse(archive, filename=archive.name, media_type="application/zip")
