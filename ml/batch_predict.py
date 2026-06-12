"""Batch demand/anomaly predictions via SageMaker Batch Transform (default) or local joblib."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import boto3
import pandas as pd
from dotenv import load_dotenv

from ml.artifact_utils import list_artifact_keys, load_joblib_from_s3, resolve_model_uri
from ml.batch_output import parse_batch_transform_body

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    load_dotenv(ROOT / ".env", override=False)
    if (ROOT / "connection.env").is_file():
        load_dotenv(ROOT / "connection.env", override=True)


def _role_arn() -> str:
    role = (os.environ.get("TASK_ROLE_ARN") or os.environ.get("SAGEMAKER_ROLE_ARN") or "").strip()
    if not role:
        sts = boto3.client("sts")
        account = sts.get_caller_identity()["Account"]
        role = f"arn:aws:iam::{account}:role/LabRole"
    return role


def _resolve_models(s3, bucket: str) -> tuple[str, str]:
    demand_uri = resolve_model_uri(
        s3, bucket, os.environ.get("SAGEMAKER_DEMAND_MODEL_NAME", ""), job_hint="dijkfood-demand"
    )
    anomaly_uri = resolve_model_uri(
        s3, bucket, os.environ.get("SAGEMAKER_ANOMALY_MODEL_NAME", ""), job_hint="dijkfood-anomaly"
    )
    if not demand_uri or not anomaly_uri:
        found = list_artifact_keys(s3, bucket)
        hints = [
            "Train models: python -m ml.prepare_datasets && python -m ml.train",
            "Or set SAGEMAKER_DEMAND_MODEL_NAME / SAGEMAKER_ANOMALY_MODEL_NAME in connection.env",
        ]
        if found:
            hints.append(f"Artifacts: {', '.join(found[-5:])}")
        else:
            hints.append(f"No model.tar.gz under s3://{bucket}/ml/artifacts/")
        raise RuntimeError("\n  ".join(hints))
    return demand_uri, anomaly_uri


def _publish_latest_jsonl(s3, bucket: str, batch_prefix: str, latest_key: str) -> int:
    """Read newest Batch Transform .out (CSV or JSON) and write dashboard-friendly JSONL."""
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=batch_prefix):
        for obj in page.get("Contents", []):
            k = obj.get("Key", "")
            if k.endswith(".out"):
                keys.append(k)
    if not keys:
        return 0
    key = sorted(keys)[-1]
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    rows = parse_batch_transform_body(body)
    if not rows:
        return 0
    out_body = "\n".join(json.dumps(r, default=str) for r in rows) + "\n"
    s3.put_object(Bucket=bucket, Key=latest_key, Body=out_body.encode("utf-8"))
    return len(rows)


def republish_latest(*, bucket: str | None = None) -> dict[str, int]:
    """Rebuild latest.jsonl from existing batch .out files (no SageMaker job)."""
    _load_env()
    bucket = bucket or os.environ.get("DATALAKE_S3_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("Set DATALAKE_S3_BUCKET in connection.env")
    region = os.environ.get("AWS_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region)
    counts: dict[str, int] = {}
    for kind in ("demand", "anomaly"):
        latest_key = f"ml/predictions/{kind}/latest.jsonl"
        counts[kind] = _publish_latest_jsonl(
            s3, bucket, f"ml/predictions/{kind}/batch/", latest_key
        )
        print(f"  {kind}: {counts[kind]} rows -> s3://{bucket}/{latest_key}")
    return counts


def run_sagemaker_batch() -> dict[str, int]:
    """Run inference in the SageMaker sklearn container (matches training sklearn version)."""
    from tools.sagemaker_infra import run_batch_transform

    _load_env()
    bucket = os.environ.get("DATALAKE_S3_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("Set DATALAKE_S3_BUCKET in connection.env")
    region = os.environ.get("AWS_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region)
    role = _role_arn()
    demand_uri, anomaly_uri = _resolve_models(s3, bucket)
    counts: dict[str, int] = {}

    for kind, model_uri in [("demand", demand_uri), ("anomaly", anomaly_uri)]:
        input_s3 = f"s3://{bucket}/ml/datasets/{kind}/train.csv"
        batch_out = f"s3://{bucket}/ml/predictions/{kind}/batch/"
        print(f"  SageMaker batch transform {kind}...")
        run_batch_transform(
            model_data=model_uri,
            input_s3=input_s3,
            output_s3=batch_out,
            role_arn=role,
            region=region,
            job_name=f"dijkfood-{kind}-batch",
            kind=kind,
        )
        latest_key = f"ml/predictions/{kind}/latest.jsonl"
        counts[kind] = _publish_latest_jsonl(
            s3, bucket, f"ml/predictions/{kind}/batch/", latest_key
        )
        print(f"  {kind}: {counts[kind]} rows -> s3://{bucket}/{latest_key}")
    return counts


def _read_csv_from_s3(s3, bucket: str, key: str) -> pd.DataFrame:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".csv") as tmp:
        s3.download_file(bucket, key, tmp.name)
        return pd.read_csv(tmp.name)


def _upload_jsonl(s3, bucket: str, key: str, rows: list[dict]) -> None:
    body = "\n".join(json.dumps(r, default=str) for r in rows) + "\n"
    s3.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"))


def run_local_batch() -> dict[str, int]:
    """Local joblib inference — requires scikit-learn version matching the training job."""
    import sklearn

    _load_env()
    bucket = os.environ.get("DATALAKE_S3_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("Set DATALAKE_S3_BUCKET in connection.env")
    region = os.environ.get("AWS_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region)
    demand_uri, anomaly_uri = _resolve_models(s3, bucket)
    counts: dict[str, int] = {}

    for kind, model_uri in [("demand", demand_uri), ("anomaly", anomaly_uri)]:
        data_key = f"ml/datasets/{kind}/train.csv"
        out_key = f"ml/predictions/{kind}/latest.jsonl"
        try:
            bundle = load_joblib_from_s3(s3, model_uri)
        except ValueError as exc:
            if "incompatible dtype" in str(exc) or "node array" in str(exc):
                raise RuntimeError(
                    f"scikit-learn version mismatch (local {sklearn.__version__}). "
                    "Re-run without --local to use SageMaker Batch Transform, or "
                    "pip install 'scikit-learn>=1.4.0,<1.5.0' and retrain: python -m ml.train"
                ) from exc
            raise
        df = _read_csv_from_s3(s3, bucket, data_key)
        if kind == "demand":
            if not isinstance(bundle, dict) or "label_encoder" not in bundle:
                raise RuntimeError("demand bundle invalid; re-run ml.train")
            le = bundle["label_encoder"]
            model = bundle["model"]
            df["region_enc"] = le.transform(df["region_grid"].astype(str))
            preds = model.predict(df[["region_enc", "hour", "weekday"]])
            rows = [{**df.iloc[i].to_dict(), "predicted_order_count": float(p)} for i, p in enumerate(preds)]
        else:
            if not isinstance(bundle, dict):
                raise RuntimeError("anomaly bundle invalid; re-run ml.train")
            model = bundle["model"]
            cols = bundle["feature_cols"]
            for c in cols:
                if c not in df.columns:
                    df[c] = 0
            scores = model.predict(df[cols].fillna(0))
            rows = [
                {
                    **{k: df.iloc[i][k] for k in df.columns if k in cols or k == "window_start"},
                    "anomaly_score": int(s),
                    "is_anomaly": bool(s == -1),
                }
                for i, s in enumerate(scores)
            ]
        _upload_jsonl(s3, bucket, out_key, rows)
        counts[kind] = len(rows)
        print(f"  {kind}: {counts[kind]} rows -> s3://{bucket}/{out_key} (local)")
    return counts


def main() -> None:
    p = argparse.ArgumentParser(description="Run demand/anomaly batch predictions")
    p.add_argument(
        "--local",
        action="store_true",
        help="Run joblib inference locally (needs matching scikit-learn; default is SageMaker Batch Transform)",
    )
    p.add_argument(
        "--republish-only",
        action="store_true",
        help="Rebuild latest.jsonl from existing batch .out files (skip SageMaker jobs)",
    )
    args = p.parse_args()
    if args.republish_only:
        counts = republish_latest()
    elif args.local:
        counts = run_local_batch()
    else:
        counts = run_sagemaker_batch()
    print("Batch prediction counts:", counts)


if __name__ == "__main__":
    main()
