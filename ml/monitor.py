"""Compare predicted vs actual delivery times and emit CloudWatch custom metrics."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import boto3
from dotenv import load_dotenv

from ml.prepare_datasets import _read_events_s3
from ml.features import delivery_features, normalize_events

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    load_dotenv(ROOT / ".env", override=False)
    if (ROOT / "connection.env").is_file():
        load_dotenv(ROOT / "connection.env", override=True)


def run() -> dict[str, float]:
    _load_env()
    bucket = os.environ.get("DATALAKE_S3_BUCKET", "").strip()
    table = os.environ.get("DYNAMO_PREDICTIONS_TABLE", "").strip()
    region = os.environ.get("AWS_REGION", "us-east-1")
    if not bucket or not table:
        return {"mae": 0.0, "samples": 0.0}

    s3 = boto3.client("s3", region_name=region)
    ddb = boto3.resource("dynamodb", region_name=region)
    events = _read_events_s3(s3, bucket)
    actuals = delivery_features(events)
    if actuals.empty:
        return {"mae": 0.0, "samples": 0.0}

    pred_table = ddb.Table(table)
    errors: list[float] = []
    for _, row in actuals.iterrows():
        oid = int(row["order_id"])
        item = pred_table.get_item(Key={"orderId": oid}).get("Item")
        if not item:
            continue
        pred = float(item.get("predicted_seconds", 0))
        errors.append(abs(pred - float(row["delivery_seconds"])))

    mae = sum(errors) / len(errors) if errors else 0.0
    cw = boto3.client("cloudwatch", region_name=region)
    cw.put_metric_data(
        Namespace="DijkFood/Predictions",
        MetricData=[
            {
                "MetricName": "DeliveryTimeMAE",
                "Value": mae,
                "Unit": "Seconds",
                "Timestamp": datetime.now(timezone.utc),
            },
            {
                "MetricName": "DeliveryTimeSamples",
                "Value": float(len(errors)),
                "Unit": "Count",
                "Timestamp": datetime.now(timezone.utc),
            },
        ],
    )
    return {"mae": mae, "samples": float(len(errors))}


if __name__ == "__main__":
    print(run())
