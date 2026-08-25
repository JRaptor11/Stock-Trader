"""Optional durable storage for historical datasets and result artifacts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ArtifactStore:
    durable = False

    def upload_file(self, path: str | Path, key: str) -> str | None:
        return None

    def download_file(self, key: str, path: str | Path) -> bool:
        return False

    def list_keys(self, prefix: str = "") -> list[str]:
        return []

    def delete_file(self, key: str) -> None:
        return None

    def total_bytes(self) -> int:
        return 0


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
    )
