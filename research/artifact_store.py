"""Optional durable storage for historical datasets and result artifacts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ArtifactStore:
    durable = False

    def upload_file(self, path: str | Path, key: str) -> str | None:
        return None


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
    durable = True

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
        self._client().upload_file(str(path), self.bucket, normalized)
        return f"s3://{self.bucket}/{normalized}"


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
    )
