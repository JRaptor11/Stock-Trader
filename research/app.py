"""Authenticated, broker-free web coordinator for historical research jobs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse

from config.service_mode import ServiceMode, validate_service_startup
from research.artifact_store import artifact_store_from_env
from research.worker import _write_json_atomic, execute_job


class ResearchRuntime:
    def __init__(self) -> None:
        self.data_root = Path(os.getenv("RESEARCH_DATA_DIR", "/tmp/research/data")).resolve()
        self.job_root = Path(os.getenv("RESEARCH_JOB_DIR", "/tmp/research/jobs")).resolve()
        self.results_root = Path(os.getenv("RESEARCH_RESULTS_DIR", "/tmp/research/results")).resolve()
        self.maximum_upload_bytes = int(os.getenv("RESEARCH_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
        self.api_token = str(os.getenv("RESEARCH_API_TOKEN") or "")
        self.active_job_id: str | None = None
        self.lock = asyncio.Lock()
        self.store = None

    def initialize(self) -> None:
        validate_service_startup(ServiceMode.HISTORICAL_RESEARCH)
        self.data_root = Path(os.getenv("RESEARCH_DATA_DIR", "/tmp/research/data")).resolve()
        self.job_root = Path(os.getenv("RESEARCH_JOB_DIR", "/tmp/research/jobs")).resolve()
        self.results_root = Path(os.getenv("RESEARCH_RESULTS_DIR", "/tmp/research/results")).resolve()
        self.maximum_upload_bytes = int(os.getenv("RESEARCH_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
        self.api_token = str(os.getenv("RESEARCH_API_TOKEN") or "")
        if len(self.api_token) < 24:
            raise RuntimeError("RESEARCH_API_TOKEN must contain at least 24 characters")
        for path in (self.data_root, self.job_root, self.results_root):
            path.mkdir(parents=True, exist_ok=True)
        self.store = artifact_store_from_env()


runtime = ResearchRuntime()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    runtime.initialize()
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


@app.get("/healthz")
def health() -> dict:
    return {
        "status": "ok",
        "service_mode": "historical_research",
        "busy": runtime.active_job_id is not None,
        "durable_storage_configured": bool(runtime.store and runtime.store.durable),
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
    durable_uri = runtime.store.upload_file(destination, f"datasets/{digest.hexdigest()}-{filename}")
    return {"filename": filename, "bytes": size, "sha256": digest.hexdigest(), "durable_uri": durable_uri}


def _run_job(job_id: str, job_path: Path) -> None:
    try:
        output = execute_job(job_path, runtime.data_root, runtime.results_root)
        archive_base = runtime.results_root / f"{job_id}-results"
        archive = Path(shutil.make_archive(str(archive_base), "zip", root_dir=output))
        result_uri = runtime.store.upload_file(archive, f"results/{archive.name}")
        status_payload = _status(job_id)
        status_payload.update({"archive": str(archive), "durable_uri": result_uri})
        _write_json_atomic(runtime.results_root / f"{job_id}.status.json", status_payload)
    except Exception:
        logging.exception("Historical research job failed: %s", job_id)
    finally:
        status_path = runtime.results_root / f"{job_id}.status.json"
        if status_path.is_file():
            try:
                runtime.store.upload_file(status_path, f"status/{status_path.name}")
            except Exception:
                logging.exception("Could not upload durable job status: %s", job_id)
        runtime.active_job_id = None


@app.post("/api/jobs", status_code=202, dependencies=[Depends(require_token)])
async def submit_job(job: dict) -> dict:
    job_id = _safe_name(str(job.get("job_id") or ""))
    bars_csv = _safe_name(str(job.get("bars_csv") or ""), ".csv")
    job = {**job, "job_id": job_id, "bars_csv": bars_csv}
    if not (runtime.data_root / bars_csv).is_file():
        raise HTTPException(status_code=400, detail="dataset has not been uploaded")
    async with runtime.lock:
        if runtime.active_job_id:
            raise HTTPException(status_code=409, detail=f"job {runtime.active_job_id} is already running")
        if (runtime.results_root / job_id).exists():
            raise HTTPException(status_code=409, detail="job results already exist")
        job_path = runtime.job_root / f"{job_id}.json"
        _write_json_atomic(job_path, job)
        job_uri = runtime.store.upload_file(job_path, f"jobs/{job_path.name}")
        runtime.active_job_id = job_id
        asyncio.create_task(asyncio.to_thread(_run_job, job_id, job_path))
    return {"job_id": job_id, "status": "accepted", "durable_job_uri": job_uri}


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
    if not archive.is_file():
        raise HTTPException(status_code=404, detail="result archive is not available")
    return FileResponse(archive, filename=archive.name, media_type="application/zip")
