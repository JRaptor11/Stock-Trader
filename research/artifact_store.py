"""Optional durable storage for historical datasets and result artifacts."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class ArtifactStore:
    durable = False

    def upload_file(self, path: str | Path, key: str) -> str | None:
        return None

    def upload_file_if_missing(self, path: str | Path, key: str) -> str | None:
        return self.upload_file(path, key)

    def download_file(self, key: str, path: str | Path) -> bool:
        return False

    def list_keys(self, prefix: str = "") -> list[str]:
        return []

    def download_url(self, key: str, expires_seconds: int = 900) -> str | None:
        return None

    def delete_file(self, key: str) -> None:
        return None

    def total_bytes(self) -> int:
        return 0

    def egress_usage(self) -> dict:
        return {"period": None, "used_bytes": 0, "budget_bytes": None}


class EgressBudgetExceeded(RuntimeError):
    """Raised before an R2 upload would exceed the configured monthly ceiling."""


class LocalOnlyArtifactStore(ArtifactStore):
    """Explicit no-op store used when durable storage is not configured."""


@dataclass
class S3ArtifactStore(ArtifactStore):
    bucket: str
    prefix: str
    endpoint_url: str | None
    region: str
    access_key: str
    secret_key: str
    monthly_egress_budget_bytes: int = 5 * 1024**3
    durable = True
    _egress_categories = ("checkpoints", "datasets", "jobs", "results", "status", "other")

    def __post_init__(self):
        self._egress_lock = threading.RLock()
        self._egress_cache = None

    def _client(self):
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
        )

    def upload_file(self, path: str | Path, key: str) -> str:
        normalized = "/".join(part for part in (self.prefix.strip("/"), key.lstrip("/")) if part)
        size = Path(path).stat().st_size
        category = key.lstrip("/").split("/", 1)[0]
        if category not in self._egress_categories:
            category = "other"
        with self._egress_lock:
            usage = self._load_egress_usage()
            projected = sum(usage["categories"].values()) + size
            if projected > self.monthly_egress_budget_bytes:
                raise EgressBudgetExceeded(
                    "monthly Render-to-R2 egress safety budget would be exceeded: "
                    f"{projected} > {self.monthly_egress_budget_bytes} bytes"
                )
            client = self._client()
            client.upload_file(str(path), self.bucket, normalized)
            usage["categories"][category] += size
            self._write_egress_usage(client, usage, category)
            self._egress_cache = usage
        return f"s3://{self.bucket}/{normalized}"

    def upload_file_if_missing(self, path: str | Path, key: str) -> str:
        """Avoid retransmitting immutable datasets/results after a restart."""
        normalized = self._normalized_key(key)
        size = Path(path).stat().st_size
        try:
            remote = self._client().head_object(Bucket=self.bucket, Key=normalized)
            if int(remote.get("ContentLength", -1)) == size:
                return f"s3://{self.bucket}/{normalized}"
        except Exception as exc:
            response = getattr(exc, "response", {})
            code = str(response.get("Error", {}).get("Code") or "")
            if code not in {"404", "NoSuchKey"} and not isinstance(exc, AttributeError):
                raise
        return self.upload_file(path, key)

    def _egress_period(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _egress_key(self, period: str, category: str) -> str:
        return self._normalized_key(f"control/egress-{period}-{category}.json")

    def _load_egress_usage(self) -> dict:
        period = self._egress_period()
        # Refresh every category from R2. The coordinator and worker are
        # separate processes, so a process-local cache could undercount the
        # other process's uploads and defeat the circuit breaker.
        categories = {}
        for category in self._egress_categories:
            try:
                response = self._client().get_object(
                    Bucket=self.bucket, Key=self._egress_key(period, category)
                )
                payload = json.loads(response["Body"].read())
                used = max(0, int(payload.get("used_bytes") or 0))
            except Exception as exc:
                response = getattr(exc, "response", {})
                code = str(response.get("Error", {}).get("Code") or "")
                if code not in {"404", "NoSuchKey", "NoSuchBucket"} and not isinstance(
                    exc, (AttributeError, KeyError)
                ):
                    raise
                used = 0
            categories[category] = used
        self._egress_cache = {"period": period, "categories": categories}
        return dict(self._egress_cache)

    def _write_egress_usage(self, client, usage: dict, category: str) -> None:
        payload = {
            "period": usage["period"],
            "category": category,
            "used_bytes": usage["categories"][category],
        }
        client.put_object(
            Bucket=self.bucket,
            Key=self._egress_key(str(usage["period"]), category),
            Body=json.dumps(payload, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
        )

    def egress_usage(self) -> dict:
        with self._egress_lock:
            usage = self._load_egress_usage()
        return {
            **usage,
            "used_bytes": sum(usage["categories"].values()),
            "budget_bytes": self.monthly_egress_budget_bytes,
        }

    def _normalized_key(self, key: str) -> str:
        return "/".join(part for part in (self.prefix.strip("/"), key.lstrip("/")) if part)

    def download_file(self, key: str, path: str | Path) -> bool:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._client().download_file(self.bucket, self._normalized_key(key), str(destination))
        except Exception as exc:
            response = getattr(exc, "response", {})
            if str(response.get("Error", {}).get("Code")) in {"404", "NoSuchKey"}:
                return False
            raise
        return True

    def download_url(self, key: str, expires_seconds: int = 900) -> str:
        return self._client().generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._normalized_key(key)},
            ExpiresIn=max(60, min(int(expires_seconds), 3600)),
        )

    def list_keys(self, prefix: str = "") -> list[str]:
        base = self.prefix.strip("/")
        requested = self._normalized_key(prefix)
        paginator = self._client().get_paginator("list_objects_v2")
        output = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=requested):
            for item in page.get("Contents", []):
                key = str(item["Key"])
                output.append(key[len(base) + 1:] if base and key.startswith(base + "/") else key)
        return output

    def delete_file(self, key: str) -> None:
        self._client().delete_object(Bucket=self.bucket, Key=self._normalized_key(key))

    def total_bytes(self) -> int:
        base = self.prefix.strip("/")
        paginator = self._client().get_paginator("list_objects_v2")
        return sum(
            int(item.get("Size", 0))
            for page in paginator.paginate(Bucket=self.bucket, Prefix=base)
            for item in page.get("Contents", [])
        )


def artifact_store_from_env() -> ArtifactStore:
    bucket = str(os.getenv("RESEARCH_S3_BUCKET") or "").strip()
    if not bucket:
        return LocalOnlyArtifactStore()
    required = {
        "RESEARCH_S3_ACCESS_KEY": os.getenv("RESEARCH_S3_ACCESS_KEY"),
        "RESEARCH_S3_SECRET_KEY": os.getenv("RESEARCH_S3_SECRET_KEY"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"durable research storage is incomplete: {', '.join(missing)}")
    return S3ArtifactStore(
        bucket=bucket,
        prefix=os.getenv("RESEARCH_S3_PREFIX", "stock-trader-research"),
        endpoint_url=os.getenv("RESEARCH_S3_ENDPOINT") or None,
        region=os.getenv("RESEARCH_S3_REGION", "us-east-1"),
        access_key=str(required["RESEARCH_S3_ACCESS_KEY"]),
        secret_key=str(required["RESEARCH_S3_SECRET_KEY"]),
        monthly_egress_budget_bytes=max(
            1024**2,
            int(os.getenv("RESEARCH_R2_MONTHLY_EGRESS_BUDGET_BYTES", str(5 * 1024**3))),
        ),
    )
