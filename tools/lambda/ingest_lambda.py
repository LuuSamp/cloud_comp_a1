import json
import os
from typing import Optional
from urllib.request import urlopen, Request
from datetime import datetime, timezone

import boto3

S3_BUCKET = os.environ.get("DATALAKE_S3_BUCKET")
ORDERING_BASE_URL = (os.environ.get("ORDERING_BASE_URL") or "").strip().rstrip("/")


def _to_py(item):
    # DynamoDB JSON (e.g. {'S': 'x'}) -> plain python
    from boto3.dynamodb.types import TypeDeserializer

    d = TypeDeserializer()
    return {k: d.deserialize(v) for k, v in item.items()}


def _to_iso(ts_val):
    if ts_val is None:
        return None
    if isinstance(ts_val, (int, float)):
        # assume epoch ms
        return datetime.fromtimestamp(float(ts_val) / 1000.0, tz=timezone.utc).isoformat()
    return str(ts_val)


def _event_type(source_arn: str) -> Optional[str]:
    if ":table/dijkfood-order-logs-" in source_arn:
        return "order_status"
    if ":table/dijkfood-courier-positions-" in source_arn:
        return "courier_position"
    return None


def _lookup_order(order_id: int) -> dict:
    if not ORDERING_BASE_URL:
        return {}
    url = f"{ORDERING_BASE_URL}/orders/{int(order_id)}"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=5) as resp:
            data = resp.read().decode("utf-8")
        payload = json.loads(data)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def handler(event, context):
    s3 = boto3.client("s3")
    records = []
    for r in event.get("Records", []):
        try:
            payload = r.get("dynamodb", {})
            new_image = payload.get("NewImage")
            if new_image is None:
                continue
            obj = _to_py(new_image)
            source_arn = r.get("eventSourceARN") or ""
            etype = _event_type(source_arn)
            # Normalize to the dashboard schema
            if etype == "order_status":
                order_payload = _lookup_order(int(obj.get("orderId") or obj.get("order_id") or 0))
                out = {
                    "type": "order_status",
                    "order_id": int(obj.get("orderId") or obj.get("order_id") or 0),
                    "status_id": int(obj.get("orderStatusId") or obj.get("order_status_id") or 0),
                    "timestamp": _to_iso(obj.get("timestamp") or obj.get("Timestamp")),
                    "detail": obj.get("detail"),
                }
                if order_payload:
                    out["food_place_id"] = order_payload.get("food_place_id")
                    out["customer_id"] = order_payload.get("customer_id")
            elif etype == "courier_position":
                out = {
                    "type": "courier_position",
                    "courier_id": int(obj.get("courierId") or obj.get("courier_id") or 0),
                    "timestamp": _to_iso(obj.get("timestamp") or obj.get("timestamp_ms") or obj.get("timestampMs")),
                    "lat": float(obj.get("lat") or obj.get("latitude") or 0),
                    "lon": float(obj.get("lon") or obj.get("longitude") or 0),
                }
            else:
                out = obj
            out["_source_event"] = {"eventID": r.get("eventID"), "eventName": r.get("eventName")}
            records.append(out)
        except Exception:
            continue
    if not records:
        return {"written": 0}
    body = "\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in records) + "\n"
    if not S3_BUCKET:
        raise RuntimeError("DATALAKE_S3_BUCKET not configured in Lambda environment")
    key = f"events/{datetime.utcnow().strftime('%Y/%m/%d/%H%M%S')}_{context.aws_request_id}.jsonl"
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=body.encode("utf-8"))
    return {"written": len(records), "s3_key": key}
