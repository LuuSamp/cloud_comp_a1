"""Prediction gateway: SageMaker delivery endpoint + batch forecast reads."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import boto3
from batch_output import parse_batch_transform_body, parse_jsonl_body
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="DijkFood Prediction Service", version="1.0.0")

REGION = os.environ.get("AWS_REGION", "us-east-1")
ENDPOINT = (os.environ.get("SAGEMAKER_DELIVERY_ENDPOINT") or "").strip()
DATALAKE_BUCKET = (os.environ.get("DATALAKE_S3_BUCKET") or "").strip()
PREDICTIONS_TABLE = (os.environ.get("DYNAMODB_PREDICTIONS_TABLE") or "").strip()
FALLBACK_SECONDS = float(os.environ.get("PREDICTION_FALLBACK_SECONDS", "1800"))


class DeliveryTimeIn(BaseModel):
    order_id: int
    food_place_id: int
    hour: int = Field(ge=0, le=23)
    weekday: int = Field(ge=0, le=6)
    customer_id: int | None = None


class DeliveryTimeOut(BaseModel):
    ok: bool
    order_id: int
    predicted_seconds: float
    source: str
    model_version: str = ""


class HealthOut(BaseModel):
    ok: bool
    endpoint_configured: bool
    datalake_bucket: str


def _runtime():
    return boto3.client("sagemaker-runtime", region_name=REGION)


def _s3():
    return boto3.client("s3", region_name=REGION)


def _ddb():
    return boto3.resource("dynamodb", region_name=REGION)


def _invoke_endpoint(features: dict[str, Any]) -> float | None:
    if not ENDPOINT:
        return None
    payload = json.dumps([features])
    try:
        resp = _runtime().invoke_endpoint(
            EndpointName=ENDPOINT,
            ContentType="application/json",
            Body=payload.encode("utf-8"),
        )
        body = json.loads(resp["Body"].read().decode("utf-8"))
        if isinstance(body, dict) and "predicted_seconds" in body:
            return float(body["predicted_seconds"])
        if isinstance(body, list) and body:
            return float(body[0])
    except ClientError:
        return None
    return None


def _heuristic(features: dict[str, Any]) -> float:
    base = FALLBACK_SECONDS
    hour = int(features.get("hour") or 12)
    if 11 <= hour <= 14 or 18 <= hour <= 21:
        base *= 1.2
    fp = int(features.get("food_place_id") or 0)
    return base + (fp % 7) * 30.0


def _persist_prediction(order_id: int, predicted_seconds: float, source: str) -> None:
    if not PREDICTIONS_TABLE:
        return
    table = _ddb().Table(PREDICTIONS_TABLE)
    now = datetime.now(timezone.utc).isoformat()
    table.put_item(
        Item={
            "orderId": order_id,
            "predicted_seconds": predicted_seconds,
            "source": source,
            "updated_at": now,
        }
    )


@app.get("/health")
def health() -> HealthOut:
    return HealthOut(
        ok=True,
        endpoint_configured=bool(ENDPOINT),
        datalake_bucket=DATALAKE_BUCKET,
    )


@app.post("/prediction/v1/delivery-time", response_model=DeliveryTimeOut)
def predict_delivery_time(body: DeliveryTimeIn) -> DeliveryTimeOut:
    features = {
        "food_place_id": body.food_place_id,
        "hour": body.hour,
        "weekday": body.weekday,
    }
    predicted = _invoke_endpoint(features)
    source = "sagemaker"
    if predicted is None:
        predicted = _heuristic(features)
        source = "heuristic"
    _persist_prediction(body.order_id, predicted, source)
    return DeliveryTimeOut(
        ok=True,
        order_id=body.order_id,
        predicted_seconds=predicted,
        source=source,
        model_version=ENDPOINT or "heuristic",
    )


@app.get("/prediction/v1/delivery-time/{order_id}")
def get_delivery_prediction(order_id: int) -> dict[str, Any]:
    if not PREDICTIONS_TABLE:
        raise HTTPException(503, detail="predictions table not configured")
    table = _ddb().Table(PREDICTIONS_TABLE)
    item = table.get_item(Key={"orderId": order_id}).get("Item")
    if not item:
        raise HTTPException(404, detail="prediction not found")
    return {"ok": True, **item}


def _latest_s3_jsonl(prefix: str) -> list[dict[str, Any]]:
    if not DATALAKE_BUCKET:
        return []
    s3 = _s3()
    latest_key = f"{prefix.rstrip('/')}/latest.jsonl"
    try:
        body = s3.get_object(Bucket=DATALAKE_BUCKET, Key=latest_key)["Body"].read().decode("utf-8")
        rows = parse_jsonl_body(body)
        if rows:
            return rows
    except ClientError:
        pass
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=DATALAKE_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            k = obj.get("Key", "")
            if k.endswith(".out"):
                keys.append(k)
    if not keys:
        return []
    key = sorted(keys)[-1]
    body = s3.get_object(Bucket=DATALAKE_BUCKET, Key=key)["Body"].read().decode("utf-8")
    return parse_batch_transform_body(body)


@app.get("/prediction/v1/demand-forecast")
def demand_forecast() -> dict[str, Any]:
    rows = _latest_s3_jsonl("ml/predictions/demand/")
    return {"ok": True, "count": len(rows), "forecasts": rows[:100]}


@app.get("/prediction/v1/anomalies")
def anomalies() -> dict[str, Any]:
    rows = _latest_s3_jsonl("ml/predictions/anomaly/")
    return {"ok": True, "count": len(rows), "anomalies": rows[:50]}


@app.get("/prediction/v1/metrics")
def metrics() -> dict[str, Any]:
    cw = boto3.client("cloudwatch", region_name=REGION)
    if not ENDPOINT:
        return {"ok": True, "endpoint": None, "invocations": None}
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/SageMaker",
            MetricName="Invocations",
            Dimensions=[{"Name": "EndpointName", "Value": ENDPOINT}],
            StartTime=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0),
            EndTime=datetime.now(timezone.utc),
            Period=3600,
            Statistics=["Sum"],
        )
        pts = resp.get("Datapoints", [])
        total = sum(p.get("Sum", 0) for p in pts)
        return {"ok": True, "endpoint": ENDPOINT, "invocations_today": total}
    except ClientError as exc:
        return {"ok": False, "error": str(exc)}
