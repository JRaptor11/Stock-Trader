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
from fastapi.responses import FileResponse, RedirectResponse

from config.service_mode import ServiceMode, validate_service_startup
from research.artifact_store import EgressBudgetExceeded, artifact_store_from_env
from research.experiment_audit import (
    AuditIntegrityError,
    HoldoutReuseError,
    assert_audit_anchors,
    audit_integrity_summary,
    register_job_audit,
)
from research.job_queue import (
    classify_failure, progress_aware_restart_state, queued_in_fifo_order,
    retry_delay_seconds,
)
from research.runtime_io import cgroup_memory_snapshot, drop_file_cache, write_json_atomic
from research.prospective_shadow_coordinator import ProspectiveShadowCoordinator


PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()
PROCESS_STARTED_MONOTONIC = time.monotonic()


class StorageBudgetExceeded(RuntimeError):
    pass


class DatasetAmbiguityError(RuntimeError):
    """Raised when a filename resolves to multiple durable dataset objects."""


class ResearchRuntime:
    def __init__(self) -> None:
        self.data_root = Path(os.getenv("RESEARCH_DATA_DIR", "/tmp/research/data")).resolve()
        self.job_root = Path(os.getenv("RESEARCH_JOB_DIR", "/tmp/research/jobs")).resolve()
        self.results_root = Path(os.getenv("RESEARCH_RESULTS_DIR", "/tmp/research/results")).resolve()
        self.audit_root = Path(os.getenv("RESEARCH_AUDIT_DIR", "/tmp/research/audit")).resolve()
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
        self.maximum_lifetime_restarts = 20
        self.retry_base_seconds = 30.0
        self.retry_max_seconds = 300.0
        self.cleanup_local_artifacts = True
        self._storage_usage_cache: tuple[float, int] | None = None
        self._health_egress_cache: tuple[float, dict] | None = None
        self._dataset_digest_cache: dict[str, tuple[int, int, str]] = {}
        self.startup_completed_at: str | None = None
        self.startup_duration_seconds: float | None = None
        self.coordinator_ready = False
        self.restore_error: str | None = None
        self.shadow_coordinator = None

    def initialize(self) -> None:
        validate_service_startup(ServiceMode.HISTORICAL_RESEARCH)
        self.data_root = Path(os.getenv("RESEARCH_DATA_DIR", "/tmp/research/data")).resolve()
        self.job_root = Path(os.getenv("RESEARCH_JOB_DIR", "/tmp/research/jobs")).resolve()
        self.results_root = Path(os.getenv("RESEARCH_RESULTS_DIR", "/tmp/research/results")).resolve()
        self.audit_root = Path(
            os.getenv("RESEARCH_AUDIT_DIR", str(self.results_root.parent / "audit"))
        ).resolve()
        self.maximum_upload_bytes = int(os.getenv("RESEARCH_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
        self.api_token = str(os.getenv("RESEARCH_API_TOKEN") or "")
        self.maximum_queued_jobs = max(1, int(os.getenv("RESEARCH_MAX_QUEUED_JOBS", "20")))
        self.status_upload_seconds = max(30.0, float(os.getenv("RESEARCH_STATUS_UPLOAD_SECONDS", "60")))
        self.storage_budget_bytes = max(
            1024**3, int(os.getenv("RESEARCH_R2_STORAGE_BUDGET_BYTES", str(8 * 1024**3)))
        )
        self.maximum_retries = max(0, int(os.getenv("RESEARCH_MAX_RETRIES", "3")))
        self.maximum_lifetime_restarts = max(
            1, int(os.getenv("RESEARCH_MAX_LIFETIME_RESTARTS", "20"))
        )
        self.retry_base_seconds = max(5.0, float(os.getenv("RESEARCH_RETRY_BASE_SECONDS", "30")))
        self.retry_max_seconds = max(
            self.retry_base_seconds, float(os.getenv("RESEARCH_RETRY_MAX_SECONDS", "300"))
        )
        self.cleanup_local_artifacts = str(
            os.getenv("RESEARCH_CLEANUP_LOCAL_ARTIFACTS", "true")
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._storage_usage_cache = None
        self._health_egress_cache = None
        self._dataset_digest_cache = {}
        self.coordinator_ready = False
        self.restore_error = None
        self.shadow_coordinator = None
        if len(self.api_token) < 24:
            raise RuntimeError("RESEARCH_API_TOKEN must contain at least 24 characters")
        for path in (self.data_root, self.job_root, self.results_root, self.audit_root):
            path.mkdir(parents=True, exist_ok=True)
        self.store = artifact_store_from_env()

    @property
    def trial_ledger_path(self) -> Path:
        return self.audit_root / "trial-ledger.jsonl"

    @property
    def holdout_registry_path(self) -> Path:
        return self.audit_root / "holdout-registry.jsonl"

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
    startup_started = time.monotonic()
    runtime.initialize()
    def restore_and_recover() -> None:
        try:
            _restore_durable_state()
            _recover_interrupted_job()
            runtime.coordinator_ready = True
        except Exception as exc:
            runtime.restore_error = f"{type(exc).__name__}: {exc}"
            logging.exception("Research durable-state recovery failed")
        finally:
            runtime.startup_duration_seconds = round(
                time.monotonic() - startup_started, 3
            )
            runtime.startup_completed_at = datetime.now(timezone.utc).isoformat()
            logging.info(
                "Research state recovery finished duration_seconds=%s ready=%s "
                "commit=%s branch=%s service_id=%s instance_id=%s deploy_id=%s",
                runtime.startup_duration_seconds, runtime.coordinator_ready,
                os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT"),
                os.getenv("RENDER_GIT_BRANCH") or os.getenv("GIT_BRANCH"),
                os.getenv("RENDER_SERVICE_ID"), os.getenv("RENDER_INSTANCE_ID"),
                os.getenv("RENDER_DEPLOY_ID"),
            )

    # R2 recovery previously blocked the ASGI lifespan for roughly 100 seconds,
    # producing false failed-deploy alerts.  Bind the health endpoint first;
    # mutation routes remain closed until recovery is complete.
    if runtime.store.durable:
        threading.Thread(target=restore_and_recover, daemon=True).start()
    else:
        restore_and_recover()
    yield


app = FastAPI(title="Stock Trader Historical Research", lifespan=lifespan)


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {runtime.api_token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid research API token")


def require_coordinator_ready() -> None:
    if not runtime.coordinator_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "durable-state recovery failed" if runtime.restore_error
                else "durable-state recovery is still in progress"
            ),
            headers={"Retry-After": "10"},
        )


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
    for attempt in range(50):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (PermissionError,json.JSONDecodeError):
            if attempt==49: raise
            time.sleep(min(.01*(attempt+1),.1))
    raise RuntimeError("unreachable status read state")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dataset_sha256(path: Path) -> str:
    """Hash a dataset once per unchanged local file.

    Several cohort jobs commonly share one large bars file.  Caching by the
    resolved path, byte size, and nanosecond mtime keeps job acceptance from
    rereading the same file while still invalidating the digest if its local
    bytes are replaced.
    """
    stat = path.stat()
    key = str(path.resolve())
    cached = runtime._dataset_digest_cache.get(key)
    identity = (int(stat.st_size), int(stat.st_mtime_ns))
    if cached and cached[:2] == identity:
        return cached[2]
    digest = _sha256_file(path)
    runtime._dataset_digest_cache[key] = (*identity, digest)
    return digest


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
    """Constant-time readiness response with no filesystem or network access."""
    return {
        "status": "ok",
        "service_mode": "historical_research",
        "git_commit": os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT"),
        "git_branch": os.getenv("RENDER_GIT_BRANCH") or os.getenv("GIT_BRANCH"),
        "busy": runtime.active_job_id is not None,
        "active_job_id": runtime.active_job_id,
        "durable_storage_configured": bool(runtime.store and runtime.store.durable),
        "coordinator_ready": runtime.coordinator_ready,
        "restore_error": runtime.restore_error,
        "process_started_at": PROCESS_STARTED_AT,
        "process_uptime_seconds": round(time.monotonic() - PROCESS_STARTED_MONOTONIC, 3),
        "startup_completed_at": runtime.startup_completed_at,
        "startup_duration_seconds": runtime.startup_duration_seconds,
        "render_service_id": os.getenv("RENDER_SERVICE_ID"),
        "render_instance_id": os.getenv("RENDER_INSTANCE_ID"),
        "render_deploy_id": os.getenv("RENDER_DEPLOY_ID"),
    }


def _health_egress_usage() -> dict | None:
    if not runtime.store:
        return None
    now = time.monotonic()
    if runtime._health_egress_cache and now - runtime._health_egress_cache[0] < 300:
        return runtime._health_egress_cache[1]
    try:
        usage=runtime.store.egress_usage()
        runtime._health_egress_cache=(now,usage)
        return usage
    except Exception:
        logging.warning("Could not read the R2 egress ledger", exc_info=True)
        return {"status": "temporarily_unavailable"}


@app.get("/api/diagnostics/runtime", dependencies=[Depends(require_token)])
def runtime_diagnostics() -> dict:
    """Potentially slower operational diagnostics kept off the health path."""
    try:
        audit = audit_integrity_summary(
            trial_ledger=runtime.trial_ledger_path,
            holdout_registry=runtime.holdout_registry_path,
        )
    except AuditIntegrityError as exc:
        audit = {"status": "invalid", "error": str(exc)}
    return {
        **health(),
        "queued_jobs": _queued_job_count(),
        "storage_budget_bytes": runtime.storage_budget_bytes,
        "r2_egress": _health_egress_usage(),
        "experiment_audit": audit,
        **cgroup_memory_snapshot(),
    }


def _shadow_coordinator() -> ProspectiveShadowCoordinator:
    if runtime.shadow_coordinator is None:
        runtime.shadow_coordinator = ProspectiveShadowCoordinator(
            runtime.results_root / "prospective-shadow", runtime.store
        )
    return runtime.shadow_coordinator


@app.get("/api/shadow/status", dependencies=[Depends(require_token), Depends(require_coordinator_ready)])
def prospective_shadow_status() -> dict:
    return _shadow_coordinator().status()


@app.post("/api/shadow/readiness", dependencies=[Depends(require_token), Depends(require_coordinator_ready)])
def create_prospective_shadow_readiness(payload: dict) -> dict:
    expected_session = str(payload.get("expected_session") or "")
    if not expected_session:
        raise HTTPException(status_code=400, detail="expected_session is required")
    try:
        return _shadow_coordinator().create_readiness_manifest(expected_session)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/shadow/sessions", dependencies=[Depends(require_token), Depends(require_coordinator_ready)])
def process_prospective_shadow_session(payload: dict) -> dict:
    required = {"session", "next_session", "source_observed_at", "code_revision", "bars"}
    missing = sorted(required.difference(payload))
    if missing:
        raise HTTPException(status_code=400, detail=f"missing shadow fields: {missing}")
    bars = payload.get("bars")
    if not isinstance(bars, dict) or not bars or len(bars) > 50:
        raise HTTPException(status_code=400, detail="shadow bars must contain 1-50 symbols")
    quotes = payload.get("opening_quote_diagnostics")
    if quotes is not None and (not isinstance(quotes, dict) or len(quotes) > 50):
        raise HTTPException(
            status_code=400,
            detail="opening_quote_diagnostics must contain at most 50 symbols",
        )
    try:
        return _shadow_coordinator().process_session(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _deprioritize_worker(process: subprocess.Popen) -> None:
    """Keep the coordinator responsive to Render's five-second health probe."""
    if os.name != "posix" or not hasattr(os,"setpriority"):
        return
    try:
        os.setpriority(os.PRIO_PROCESS,process.pid,10)
    except (AttributeError,OSError,PermissionError):
        logging.warning("Could not lower research worker CPU priority",exc_info=True)


@app.put("/api/datasets/{filename}", dependencies=[Depends(require_token), Depends(require_coordinator_ready)])
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
    durable_uri = runtime.store.upload_file_if_missing(
        destination, f"datasets/{digest.hexdigest()}-{filename}"
    )
    stat = destination.stat()
    runtime._dataset_digest_cache[str(destination.resolve())] = (
        int(stat.st_size), int(stat.st_mtime_ns), digest.hexdigest(),
    )
    runtime.record_storage_write(destination.stat().st_size)
    return {"filename": filename, "bytes": size, "sha256": digest.hexdigest(), "durable_uri": durable_uri}


def _run_job(job_id: str, job_path: Path) -> None:
    succeeded = False
    retry_delay = None
    try:
        output = runtime.results_root / job_id
        archive = runtime.results_root / f"{job_id}-results.zip"
        status_path = runtime.results_root / f"{job_id}.status.json"
        if not archive.is_file() and not (output / "replay_manifest.json").is_file():
            command = [
                sys.executable, "-m", "research.worker", "--job", str(job_path),
                "--data-root", str(runtime.data_root), "--results-root", str(runtime.results_root),
            ]
            process = subprocess.Popen(command, cwd=str(Path(__file__).resolve().parents[1]))
            _deprioritize_worker(process)
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
        # New workers write the final archive directly to avoid holding an
        # expanded result tree and its ZIP simultaneously. Preserve the legacy
        # directory path so in-flight deployments from older commits can still
        # be finalized safely.
        if not archive.is_file():
            if not output.is_dir():
                raise RuntimeError("research worker completed without a result archive")
            archive_base = runtime.results_root / f"{job_id}-results"
            archive = Path(shutil.make_archive(str(archive_base), "zip", root_dir=output))
        runtime.ensure_storage_capacity(archive)
        result_uri = runtime.store.upload_file_if_missing(
            archive, f"results/{archive.name}"
        )
        drop_file_cache(archive)
        runtime.record_storage_write(archive.stat().st_size)
        status_payload = _status(job_id)
        status_payload.update({
            "status": "complete", "archive": str(archive), "durable_uri": result_uri,
            "completed_at": status_payload.get("completed_at") or datetime.now(timezone.utc).isoformat(),
        })
        write_json_atomic(runtime.results_root / f"{job_id}.status.json", status_payload)
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
        retryable, failure_class = _classify_failure(exc, previous)
        retry_state = progress_aware_restart_state(
            previous, maximum_consecutive=runtime.maximum_retries,
            maximum_lifetime=runtime.maximum_lifetime_restarts,
        ) if retryable else {}
        if retryable and retry_state["retry_allowed"]:
            retry_delay = retry_delay_seconds(
                max(1, retry_state["consecutive_restarts_without_progress"]),
                maximum_retries=runtime.maximum_retries,
                base_seconds=runtime.retry_base_seconds,
                maximum_seconds=runtime.retry_max_seconds,
            )
            next_retry = datetime.now(timezone.utc) + timedelta(seconds=retry_delay)
            write_json_atomic(status_path, {
                **previous, **retry_state, "job_id": job_id, "status": "retry_wait",
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc), "failure_class": failure_class,
                "retryable": True, "next_retry_at": next_retry.isoformat(),
                "retries_remaining": max(
                    0, runtime.maximum_retries
                    - retry_state["consecutive_restarts_without_progress"] + 1
                ),
                "blocks_queue": False,
            })
        else:
            write_json_atomic(status_path, {
                **previous, **retry_state, "job_id": job_id, "status": "failed",
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
            runtime.store.delete_file(f"checkpoints/{job_id}/manifest.json")
            runtime.store.delete_prefix(f"checkpoints/{job_id}/parts/")
            runtime.store.delete_file(f"checkpoints/{job_id}-intraday.json")
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
        error_type,
        storage_budget_exceeded=isinstance(
            exc, (StorageBudgetExceeded, EgressBudgetExceeded)
        ),
    )


def _restore_durable_state() -> None:
    if not runtime.store.durable:
        _validate_restored_audit_state()
        return
    for key in runtime.store.list_keys("status/"):
        if key.endswith(".status.json"):
            runtime.store.download_file(key, runtime.results_root / Path(key).name)
    for key in runtime.store.list_keys("jobs/"):
        if key.endswith(".json"):
            runtime.store.download_file(key, runtime.job_root / Path(key).name)
    for key in runtime.store.list_keys("audit/"):
        if key in {"audit/trial-ledger.jsonl", "audit/holdout-registry.jsonl"}:
            runtime.store.download_file(key, runtime.audit_root / Path(key).name)
    # A corrupt chain and a valid-but-older prefix both prevent the
    # coordinator from accepting work.  Read-only health remains available
    # with the exact recovery error, matching the existing fail-closed startup.
    _validate_restored_audit_state()


def _validate_restored_audit_state() -> dict:
    """Validate chains and require every restored status/job audit anchor.

    A hash chain by itself cannot distinguish a complete ledger from a valid
    prefix.  Accepted status documents and durable job definitions therefore
    retain the chain tips observed at acceptance.  Requiring all of those tips
    makes an accidental R2 rollback or truncated overwrite detectable.
    """
    trial_anchors: list[str] = []
    holdout_anchors: list[str] = []

    def collect(payload: dict, *, field: str, source: Path) -> None:
        audit = payload.get(field)
        if audit is None:
            return  # Legacy jobs predate the audit layer.
        if not isinstance(audit, dict):
            raise AuditIntegrityError(
                f"{source.name} contains malformed experiment audit metadata"
            )
        trial = audit.get("trial_chain_sha256")
        holdout = audit.get("holdout_chain_sha256")
        if not trial:
            raise AuditIntegrityError(
                f"{source.name} is missing its trial audit anchor"
            )
        trial_anchors.append(str(trial))
        if holdout:
            holdout_anchors.append(str(holdout))

    for path in runtime.results_root.glob("*.status.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuditIntegrityError(
                f"{path.name} cannot be read while validating audit anchors"
            ) from exc
        if not isinstance(payload, dict):
            raise AuditIntegrityError(f"{path.name} is not a JSON object")
        collect(payload, field="experiment_audit", source=path)

    for path in runtime.job_root.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuditIntegrityError(
                f"{path.name} cannot be read while validating audit anchors"
            ) from exc
        if not isinstance(payload, dict):
            raise AuditIntegrityError(f"{path.name} is not a JSON object")
        collect(payload, field="_experiment_audit", source=path)

    summary = audit_integrity_summary(
        trial_ledger=runtime.trial_ledger_path,
        holdout_registry=runtime.holdout_registry_path,
    )
    anchors = assert_audit_anchors(
        trial_ledger=runtime.trial_ledger_path,
        holdout_registry=runtime.holdout_registry_path,
        required_trial_anchors=trial_anchors,
        required_holdout_anchors=holdout_anchors,
    )
    return {**summary, "anchors": anchors}


def _register_and_persist_job_audit(job: dict, accepted_at: str) -> dict:
    """Record a submission before queueing it and durably anchor both chains."""
    try:
        # Recheck persisted acceptance anchors on every submission, not only
        # after a service restart, so a mid-process valid-prefix replacement
        # cannot become the base of a new chain.
        _validate_restored_audit_state()
        result = register_job_audit(
            job=job,
            trial_ledger=runtime.trial_ledger_path,
            holdout_registry=runtime.holdout_registry_path,
            accepted_at=accepted_at,
        )
    except HoldoutReuseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AuditIntegrityError as exc:
        raise HTTPException(
            status_code=503, detail=f"experiment audit integrity failure: {exc}",
        ) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=503, detail=f"experiment audit could not be recorded: {exc}",
        ) from exc

    durable_uris = {}
    if runtime.store.durable:
        try:
            # Persist holdout consumption first.  If the process stops between
            # the two objects, evidence may be conservatively marked consumed,
            # but it can never remain labeled untouched.  Size-aware uploads
            # avoid retransmitting unchanged ledgers on an idempotent retry.
            for label, path in (
                ("holdout_registry", runtime.holdout_registry_path),
                ("trial_ledger", runtime.trial_ledger_path),
            ):
                if path.is_file():
                    runtime.ensure_storage_capacity(path)
                    durable_uris[label] = runtime.store.upload_file_if_missing(
                        path, f"audit/{path.name}",
                    )
                    # This intentionally over-reserves when an existing key is
                    # replaced.  A conservative false stop is preferable to
                    # exceeding the configured free-tier storage ceiling.
                    runtime.record_storage_write(path.stat().st_size)
        except EgressBudgetExceeded as exc:
            raise HTTPException(status_code=507, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"experiment audit could not be persisted durably: {exc}",
            ) from exc
    return {
        "accepted_at": accepted_at,
        "trial_chain_sha256": result["trials"].get("chain_sha256"),
        "job_definition_sha256": result["trials"].get("job_definition_sha256"),
        "trial_records_added": result["trials"].get("new_records", 0),
        "variant_enumeration_complete": result["trials"].get(
            "variant_enumeration_complete", False
        ),
        "holdout_status": result["holdout"].get("status"),
        "holdout_chain_sha256": result["holdout"].get("chain_sha256"),
        "durable_uris": durable_uris,
    }


def _restore_dataset(filename: str, expected_sha256: str | None = None) -> bool:
    destination = runtime.data_root / filename
    if destination.is_file():
        if expected_sha256 and _dataset_sha256(destination) != expected_sha256:
            raise DatasetAmbiguityError(
                f"local dataset {filename} does not match the job's expected SHA-256"
            )
        return True
    if not runtime.store.durable:
        return False
    matches = sorted(
        key for key in runtime.store.list_keys("datasets/")
        if key.endswith("-" + filename)
    )
    if expected_sha256:
        matches = [
            key for key in matches
            if Path(key).name == f"{expected_sha256}-{filename}"
        ]
    elif len(matches) > 1:
        raise DatasetAmbiguityError(
            f"multiple durable datasets use {filename}; an expected SHA-256 is required"
        )
    if len(matches) > 1:
        raise DatasetAmbiguityError(
            f"multiple durable objects match {filename} and SHA-256 {expected_sha256}"
        )
    if not matches:
        return False
    selected = matches[0]
    key_name = Path(selected).name
    suffix = "-" + filename
    key_digest = key_name[:-len(suffix)] if key_name.endswith(suffix) else ""
    if (
        len(key_digest) != 64
        or any(character not in "0123456789abcdef" for character in key_digest.lower())
    ):
        raise DatasetAmbiguityError(
            f"durable dataset key for {filename} does not contain a valid SHA-256"
        )
    if not runtime.store.download_file(selected, destination):
        return False
    actual = _dataset_sha256(destination)
    if actual != key_digest.lower() or (expected_sha256 and actual != expected_sha256):
        destination.unlink(missing_ok=True)
        runtime._dataset_digest_cache.pop(str(destination.resolve()), None)
        raise DatasetAmbiguityError(
            f"durable dataset bytes for {filename} do not match their SHA-256 identity"
        )
    return True


def _cleanup_local_job_artifacts(job_id: str, job_path: Path) -> None:
    """Keep Render's ephemeral filesystem as a cache; R2 remains authoritative."""
    try:
        job = json.loads(job_path.read_text(encoding="utf-8")) if job_path.is_file() else {}
        for dataset_name in (str(job.get("bars_csv") or ""),str(job.get("security_master_csv") or ""),str(job.get("market_events_csv") or ""),str(job.get("fundamentals_csv") or "")):
            if not dataset_name: continue
            dataset = runtime.data_root / Path(dataset_name).name
            if dataset.is_file():
                dataset.unlink()
                runtime._dataset_digest_cache.pop(str(dataset.resolve()), None)
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


_JOB_DATASET_FIELDS = (
    "bars_csv", "security_master_csv", "market_events_csv", "fundamentals_csv",
)


def _bind_job_dataset_hashes(job: dict) -> dict:
    """Resolve and bind every referenced dataset to its exact local bytes."""
    supplied = job.get("dataset_sha256") or {}
    if not isinstance(supplied, dict):
        raise HTTPException(status_code=400, detail="dataset_sha256 must be a JSON object")
    unexpected = sorted(set(supplied).difference(_JOB_DATASET_FIELDS))
    if unexpected:
        raise HTTPException(
            status_code=400,
            detail=f"dataset_sha256 has unexpected fields: {unexpected}",
        )
    normalized: dict[str, str] = {}
    for field in _JOB_DATASET_FIELDS:
        filename = str(job.get(field) or "").strip()
        if not filename:
            continue
        expected = str(supplied.get(field) or "").strip().lower() or None
        if expected and (
            len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
        ):
            raise HTTPException(
                status_code=400,
                detail=f"dataset_sha256.{field} must be a 64-character hexadecimal digest",
            )
        try:
            restored = _restore_dataset(filename, expected_sha256=expected)
        except DatasetAmbiguityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not restored:
            raise HTTPException(status_code=400, detail=f"{field} has not been uploaded")
        actual = _dataset_sha256(runtime.data_root / filename)
        if expected and actual != expected:
            raise HTTPException(
                status_code=409,
                detail=f"{field} does not match its declared SHA-256",
            )
        normalized[field] = actual
    return {**job, "dataset_sha256": normalized}


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
    hashes = job.get("dataset_sha256") or {}
    for field in _JOB_DATASET_FIELDS:
        filename = str(job.get(field) or "")
        if not filename:
            continue
        expected = str(hashes.get(field) or "").strip().lower() or None
        if not _restore_dataset(filename, expected_sha256=expected):
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
    durable_checkpoint = int(previous.get("checkpoint_completed_timestamps") or 0)
    write_json_atomic(status_path, {
        **previous, "job_id": job_id, "status": "starting",
        "started_by_coordinator_at": datetime.now(timezone.utc).isoformat(),
        "attempt_count": int(previous.get("attempt_count") or 0) + 1,
        "attempt_started_checkpoint_timestamps": durable_checkpoint,
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
                write_json_atomic(status_path, {
                    **payload, "status": "failed", "blocks_queue": True,
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "error": "durable job definition is missing",
                })
                return
            if _launch_job(job_id, job_path):
                return
            status_path = runtime.results_root / f"{job_id}.status.json"
            write_json_atomic(status_path, {
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
    status_path = runtime.results_root / f"{job_id}.status.json"
    retry_state = progress_aware_restart_state(
        payload, maximum_consecutive=runtime.maximum_retries,
        maximum_lifetime=runtime.maximum_lifetime_restarts,
    )
    if not retry_state["retry_allowed"]:
        write_json_atomic(status_path, {
            **payload, **retry_state, "status": "failed", "blocks_queue": True,
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "failure_class": "interrupted_service_restart",
            "error": "progress-aware restart retry allowance exhausted",
            "retryable": True, "retries_exhausted": True,
            "retries_remaining": 0, "next_retry_at": None,
        })
        runtime.store.upload_file(status_path, f"status/{status_path.name}")
        return
    delay = retry_delay_seconds(
        max(1, retry_state["consecutive_restarts_without_progress"]),
        maximum_retries=runtime.maximum_retries,
        base_seconds=runtime.retry_base_seconds,
        maximum_seconds=runtime.retry_max_seconds,
    )
    write_json_atomic(status_path, {
        **payload, **retry_state, "status": "retry_wait", "blocks_queue": False,
        "failure_class": "interrupted_service_restart", "retryable": True,
        "next_retry_at": (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(),
        "retries_remaining": max(
            0, runtime.maximum_retries
            - retry_state["consecutive_restarts_without_progress"] + 1
        ),
    })
    runtime.store.upload_file(status_path, f"status/{status_path.name}")
    _start_next_job()


@app.post("/api/jobs", status_code=202, dependencies=[Depends(require_token), Depends(require_coordinator_ready)])
async def submit_job(job: dict) -> dict:
    job_id = _safe_name(str(job.get("job_id") or ""))
    bars_csv = _safe_name(str(job.get("bars_csv") or ""), ".csv")
    security_master_csv=_safe_name(str(job["security_master_csv"]),".csv") if job.get("security_master_csv") else None
    market_events_csv=_safe_name(str(job["market_events_csv"]),".csv") if job.get("market_events_csv") else None
    fundamentals_csv=_safe_name(str(job["fundamentals_csv"]),".csv") if job.get("fundamentals_csv") else None
    job = {**job, "job_id": job_id, "bars_csv": bars_csv}
    if security_master_csv: job["security_master_csv"]=security_master_csv
    if market_events_csv: job["market_events_csv"]=market_events_csv
    if fundamentals_csv: job["fundamentals_csv"]=fundamentals_csv
    job_path = runtime.job_root / f"{job_id}.json"
    # On an idempotent retry after restart, use the already accepted hashes to
    # select the exact R2 objects even when several datasets share a filename.
    if job_path.is_file():
        existing_job = json.loads(job_path.read_text(encoding="utf-8"))
        existing_hashes = existing_job.get("dataset_sha256") or {}
        supplied_hashes = job.get("dataset_sha256") or {}
        if isinstance(existing_hashes, dict) and isinstance(supplied_hashes, dict):
            job["dataset_sha256"] = {**existing_hashes, **supplied_hashes}
    job = _bind_job_dataset_hashes(job)
    async with runtime.lock:
        status_path = runtime.results_root / f"{job_id}.status.json"
        if job_path.is_file() and not (runtime.results_root / job_id).exists():
            existing_job = json.loads(job_path.read_text(encoding="utf-8"))
            existing_public = {
                key: value for key, value in existing_job.items()
                if not str(key).startswith("_")
            }
            submitted_public = {
                key: value for key, value in job.items()
                if not str(key).startswith("_")
            }
            if existing_public != submitted_public:
                raise HTTPException(status_code=409, detail="job id already exists")
            audit = existing_job.get("_experiment_audit")
            if not isinstance(audit, dict) or not audit.get("trial_chain_sha256"):
                raise HTTPException(
                    status_code=503,
                    detail="existing job is missing its experiment audit anchor",
                )
            try:
                _validate_restored_audit_state()
            except AuditIntegrityError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"experiment audit integrity failure: {exc}",
                ) from exc
            current_status = (
                json.loads(status_path.read_text(encoding="utf-8"))
                if status_path.is_file() else None
            )
            if current_status and current_status.get("status") not in {
                "queued", "starting", "running", "retry_wait",
            }:
                raise HTTPException(status_code=409, detail="job id already exists")
            queued_at = str(
                (current_status or {}).get("queued_at")
                or audit.get("accepted_at")
                or datetime.now(timezone.utc).isoformat()
            )
            if not current_status:
                current_status = {
                    "job_id": job_id, "status": "queued", "queued_at": queued_at,
                    "bars_csv": bars_csv,
                    "security_master_csv": security_master_csv,
                    "market_events_csv": market_events_csv,
                    "fundamentals_csv": fundamentals_csv,
                    "experiment_audit": audit,
                }
                write_json_atomic(status_path, current_status)
            job_uri = runtime.store.upload_file_if_missing(
                job_path, f"jobs/{job_path.name}"
            )
            runtime.store.upload_file(status_path, f"status/{status_path.name}")
            _start_next_job()
            return {
                "job_id": job_id,
                "status": str(current_status.get("status") or "queued"),
                "queue_position": 0 if runtime.active_job_id == job_id else None,
                "durable_job_uri": job_uri,
                "experiment_audit": audit,
                "idempotent_retry": True,
            }
        if _queued_job_count() >= runtime.maximum_queued_jobs:
            raise HTTPException(status_code=429, detail="research queue is full")
        if (runtime.results_root / job_id).exists() or job_path.exists():
            raise HTTPException(status_code=409, detail="job id already exists")
        queued_at = datetime.now(timezone.utc).isoformat()
        audit = _register_and_persist_job_audit(job, queued_at)
        # The private anchor is ignored by strategy workers and immutable job
        # hashing, but lets recovery detect a valid-prefix ledger rollback even
        # if a crash happened before the first status object was uploaded.
        durable_job = {**job, "_experiment_audit": audit}
        write_json_atomic(job_path, durable_job)
        job_uri = runtime.store.upload_file(job_path, f"jobs/{job_path.name}")
        write_json_atomic(status_path, {
            "job_id": job_id, "status": "queued", "queued_at": queued_at,
            "bars_csv": bars_csv, "security_master_csv": security_master_csv,
            "market_events_csv": market_events_csv,
            "fundamentals_csv": fundamentals_csv,
            "experiment_audit": audit,
        })
        runtime.store.upload_file(status_path, f"status/{status_path.name}")
        _start_next_job()
        queued_ids = [item["job_id"] for item in _queued_statuses()]
        queue_position = 0 if runtime.active_job_id == job_id else queued_ids.index(job_id) + 1
    return {
        "job_id": job_id, "status": "starting" if queue_position == 0 else "queued",
        "queue_position": queue_position, "durable_job_uri": job_uri,
        "experiment_audit": audit,
    }


@app.post("/api/queue/resume", dependencies=[Depends(require_token), Depends(require_coordinator_ready)])
def resume_queue() -> dict:
    cleared = []
    with runtime.queue_lock:
        for path in runtime.results_root.glob("*.status.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not payload.pop("blocks_queue", None):
                continue
            payload["queue_block_cleared_at"] = datetime.now(timezone.utc).isoformat()
            write_json_atomic(path, payload)
            runtime.store.upload_file(path, f"status/{path.name}")
            cleared.append(str(payload.get("job_id") or path.stem))
        _start_next_job()
    return {"status": "resumed", "cleared_failures": cleared, "active_job_id": runtime.active_job_id}


@app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(require_token), Depends(require_coordinator_ready)])
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
            "lifetime_restart_count": int(
                payload.get("lifetime_restart_count")
                or payload.get("lifetime_attempt_count")
                or prior_attempts
            ),
            "consecutive_restarts_without_progress": 0,
            "blocks_queue": False, "next_retry_at": None,
            "retries_remaining": runtime.maximum_retries,
            "retries_exhausted": False,
        })
        for stale_key in ("error", "error_type", "failure_class", "failed_at"):
            payload.pop(stale_key, None)
        write_json_atomic(status_path, payload)
        runtime.store.upload_file(status_path, f"status/{status_path.name}")
        _start_next_job()
    return {
        "job_id": job_id,
        "status": "starting" if runtime.active_job_id == job_id else "queued",
        "active_job_id": runtime.active_job_id,
        "manual_retry_count": payload["manual_retry_count"],
        "lifetime_attempt_count": payload["lifetime_attempt_count"],
    }


@app.post("/api/jobs/{job_id}/supersede", dependencies=[Depends(require_token), Depends(require_coordinator_ready)])
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
        write_json_atomic(status_path, payload)
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
    if runtime.store.durable:
        signed_url = runtime.store.download_url(f"results/{archive.name}")
        if signed_url:
            return RedirectResponse(signed_url, status_code=307)
    if not archive.is_file() and runtime.store.durable:
        runtime.store.download_file(f"results/{archive.name}", archive)
    if not archive.is_file():
        raise HTTPException(status_code=404, detail="result archive is not available")
    return FileResponse(archive, filename=archive.name, media_type="application/zip")
