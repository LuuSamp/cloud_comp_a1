"""Resolve and load SageMaker sklearn artifacts from S3 (model.tar.gz or model.joblib)."""

from __future__ import annotations

import os
import tarfile
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
import joblib


def parse_s3_uri(uri: str) -> tuple[str, str]:
    p = urlparse(uri)
    if p.scheme != "s3" or not p.netloc:
        raise ValueError(f"Not an s3 URI: {uri}")
    return p.netloc, p.path.lstrip("/")


def latest_model_tarball_uri(
    s3,
    bucket: str,
    *,
    prefix: str = "ml/artifacts/",
    job_hint: str,
) -> str | None:
    """Return s3:// URI of the newest model.tar.gz whose key contains job_hint."""
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            k = obj.get("Key", "")
            if k.endswith("model.tar.gz") and job_hint in k:
                keys.append(k)
    if not keys:
        return None
    return f"s3://{bucket}/{sorted(keys)[-1]}"


def resolve_model_uri(
    s3,
    bucket: str,
    env_value: str,
    *,
    job_hint: str,
    prefix: str = "ml/artifacts/",
) -> str | None:
    """Use connection.env value or auto-discover latest tarball for a training job."""
    raw = (env_value or "").strip()
    if raw:
        if raw.startswith("s3://"):
            return raw
        return f"s3://{bucket}/{raw.lstrip('/')}"
    return latest_model_tarball_uri(s3, bucket, prefix=prefix, job_hint=job_hint)


def load_joblib_from_s3(s3, model_uri: str) -> Any:
    """Download model.tar.gz (SageMaker default) or model.joblib and return joblib payload."""
    bucket, key = parse_s3_uri(model_uri)
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        local = td_path / Path(key).name
        s3.download_file(bucket, key, str(local))
        if key.endswith(".tar.gz"):
            with tarfile.open(local, "r:gz") as tar:
                tar.extractall(td_path)
            candidates = list(td_path.rglob("model.joblib"))
            if not candidates:
                raise FileNotFoundError(f"model.joblib not found inside {model_uri}")
            return joblib.load(candidates[0])
        return joblib.load(local)


def list_artifact_keys(s3, bucket: str, prefix: str = "ml/artifacts/", limit: int = 15) -> list[str]:
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            k = obj.get("Key", "")
            if k.endswith("model.tar.gz"):
                keys.append(k)
    return sorted(keys)[-limit:]
