import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from research.artifact_store import LocalOnlyArtifactStore, S3ArtifactStore, artifact_store_from_env


class _FakeS3Client:
    def __init__(self):
        self.uploads = []
        self.downloads = []

    def upload_file(self, path, bucket, key):
        self.uploads.append((path, bucket, key))

    def download_file(self, bucket, key, path):
        self.downloads.append((bucket, key, path))
        Path(path).write_bytes(b"restored")

    def get_paginator(self, name):
        client = self
        class Paginator:
            def paginate(self, **kwargs):
                return [{"Contents": [{
                    "Key": "research/runs/status/job.status.json", "Size": 123,
                }]}]
        return Paginator()


class ArtifactStoreTests(unittest.TestCase):
    def test_local_store_is_used_without_bucket(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(artifact_store_from_env(), LocalOnlyArtifactStore)

    def test_bucket_configuration_requires_credentials(self):
        with patch.dict(os.environ, {"RESEARCH_S3_BUCKET": "results"}, clear=True):
            with self.assertRaises(RuntimeError):
                artifact_store_from_env()

    def test_s3_store_uploads_to_prefixed_key(self):
        client = _FakeS3Client()
        fake_boto3 = types.SimpleNamespace(client=lambda *args, **kwargs: client)
        store = S3ArtifactStore(
            bucket="results", prefix="research/runs", endpoint_url=None,
            region="us-east-1", access_key="key", secret_key="secret",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.zip"
            path.write_bytes(b"test")
            with patch.dict(sys.modules, {"boto3": fake_boto3}):
                uri = store.upload_file(path, "completed/result.zip")
        self.assertEqual(uri, "s3://results/research/runs/completed/result.zip")
        self.assertEqual(client.uploads[0][1:], ("results", "research/runs/completed/result.zip"))

    def test_s3_store_lists_and_downloads_prefixed_keys(self):
        client = _FakeS3Client()
        fake_boto3 = types.SimpleNamespace(client=lambda *args, **kwargs: client)
        store = S3ArtifactStore(
            bucket="results", prefix="research/runs", endpoint_url=None,
            region="us-east-1", access_key="key", secret_key="secret",
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "job.status.json"
            with patch.dict(sys.modules, {"boto3": fake_boto3}):
                keys = store.list_keys("status/")
                restored = store.download_file(keys[0], destination)
                total_bytes = store.total_bytes()
            self.assertTrue(restored)
            self.assertEqual(destination.read_bytes(), b"restored")
        self.assertEqual(keys, ["status/job.status.json"])
        self.assertEqual(total_bytes, 123)


if __name__ == "__main__":
    unittest.main()
