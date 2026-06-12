"""Build ML training datasets from S3 analytics events and upload to datalake."""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path

import boto3
import pandas as pd
from dotenv import load_dotenv

from ml.features import (
    anomaly_features,
    delivery_features,
    demand_features,
    normalize_events,
)

ROOT = Path(__file__).resolve().parent.parent
MIN_DELIVERY_ROWS = 20


def _load_env() -> None:
    load_dotenv(ROOT / ".env", override=False)
    if (ROOT / "connection.env").is_file():
        load_dotenv(ROOT / "connection.env", override=True)


def _read_events_s3(s3, bucket: str, prefix: str = "events/") -> pd.DataFrame:
    rows: list[dict] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if not key.endswith(".jsonl"):
                continue
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
            for line in body.splitlines():
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    return normalize_events(rows)


def _upload_csv(s3, bucket: str, key: str, df: pd.DataFrame) -> None:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    s3.put_object(Bucket=bucket, Key=key, Body=buf.getvalue().encode("utf-8"))
    print(f"  uploaded s3://{bucket}/{key} ({len(df)} rows)")


def prepare_all(*, bucket: str | None = None) -> dict[str, int]:
    _load_env()
    bucket = bucket or os.environ.get("DATALAKE_S3_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("Set DATALAKE_S3_BUCKET in connection.env")
    region = os.environ.get("AWS_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region)
    events = _read_events_s3(s3, bucket)
    if events.empty:
        print("No events found in datalake; run load test first.")
        return {"delivery": 0, "demand": 0, "anomaly": 0}

    delivery_df = delivery_features(events)
    demand_df = demand_features(events)
    anomaly_df = anomaly_features(events)

    counts = {"delivery": len(delivery_df), "demand": len(demand_df), "anomaly": len(anomaly_df)}
    if not delivery_df.empty:
        _upload_csv(s3, bucket, "ml/datasets/delivery/train.csv", delivery_df)
    if not demand_df.empty:
        _upload_csv(s3, bucket, "ml/datasets/demand/train.csv", demand_df)
    if not anomaly_df.empty:
        _upload_csv(s3, bucket, "ml/datasets/anomaly/train.csv", anomaly_df)
    return counts


def main() -> None:
    p = argparse.ArgumentParser(description="Prepare ML datasets from analytics events")
    p.add_argument("--bucket", default="", help="Override DATALAKE_S3_BUCKET")
    args = p.parse_args()
    bucket = args.bucket.strip() or None
    counts = prepare_all(bucket=bucket)
    print("Dataset row counts:", counts)
    if counts["delivery"] < MIN_DELIVERY_ROWS:
        print(
            f"WARNING: fewer than {MIN_DELIVERY_ROWS} delivery rows; "
            "training will use heuristic fallback until more data is collected."
        )


if __name__ == "__main__":
    main()
