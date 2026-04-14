"""DijkFood tracking service. ALB path prefix: /tracking"""

from __future__ import annotations

import datetime as dt
import os
import time
from decimal import Decimal
from typing import Any

import boto3
import httpx
import psycopg
from boto3.dynamodb.conditions import Key
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field

app = FastAPI(title="DijkFood tracking")


def _db_conn_str() -> str:
    host = (os.environ.get("DB_HOST") or "").strip()
    if not host:
        raise RuntimeError("DB_HOST is not configured")
    return (
        f"host={host} port={os.environ.get('DB_PORT', '5432')} "
        f"dbname={os.environ.get('DB_NAME', '')} "
        f"user={os.environ.get('DB_USER', '')} "
        f"password={os.environ.get('DB_PASSWORD', '')} "
        "connect_timeout=10"
    )


def _ddb_resource() -> Any:
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    return boto3.resource("dynamodb", region_name=region)


def _order_logs_table() -> Any:
    name = (os.environ.get("DYNAMODB_ORDER_LOGS_TABLE") or "").strip()
    if not name:
        raise RuntimeError("DYNAMODB_ORDER_LOGS_TABLE is not configured")
    return _ddb_resource().Table(name)


def _courier_positions_table() -> Any:
    name = (os.environ.get("DYNAMODB_COURIER_POSITIONS_TABLE") or "").strip()
    if not name:
        raise RuntimeError("DYNAMODB_COURIER_POSITIONS_TABLE is not configured")
    return _ddb_resource().Table(name)


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in item.items():
        if isinstance(v, Decimal):
            out[k] = int(v) if v % 1 == 0 else float(v)
        else:
            out[k] = v
    return out


def _ordering_base_url() -> str | None:
    for key in ("ORDERING_BASE_URL", "BASE_URL", "ROUTING_BASE_URL"):
        value = (os.environ.get(key) or "").strip().rstrip("/")
        if value:
            return value
    return None


def _try_assign_courier(order_id: int) -> None:
    base = _ordering_base_url()
    if not base:
        return
    timeout = httpx.Timeout(30.0, connect=5.0)
    with httpx.Client(timeout=timeout) as client:
        try:
            client.post(f"{base}/assign-courier", json={"order_id": order_id})
        except httpx.HTTPError:
            return


@app.get("/health")
def alb_health() -> dict[str, str]:
    return {"status": "ok", "service": "tracking"}


tracking = FastAPI(title="DijkFood tracking API")


class UpdateOrderStatusIn(BaseModel):
    order_id: int
    order_status_id: int = Field(..., ge=1, le=6)
    detail: str | None = None


class UpdateCourierPositionIn(BaseModel):
    courier_id: int
    timestamp: int | None = None
    position: str
    lat: float
    lon: float


class UpdateCourierStatusIn(BaseModel):
    courier_id: int
    status: str


@tracking.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "tracking"}


@tracking.get("/")
def root() -> dict[str, str]:
    return {"service": "tracking", "detail": "ready"}


@tracking.post("/update-order-status")
def update_order_status(body: UpdateOrderStatusIn) -> dict[str, Any]:
    if body.order_status_id <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="order_status_id must be >= 2")
    prev = body.order_status_id - 1
    conn_str = _db_conn_str()
    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE orders
                SET order_status_id = %(next)s
                WHERE order_id = %(order_id)s AND order_status_id = %(prev)s
                RETURNING order_id
                """,
                {"order_id": body.order_id, "prev": prev, "next": body.order_status_id},
            )
            updated = cur.fetchone()
        conn.commit()
    if not updated:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="transition rejected: order is not at prior status",
        )

    item = {
        "orderId": body.order_id,
        "timestamp": _iso_now(),
        "orderStatusId": body.order_status_id,
    }
    if body.detail:
        item["detail"] = body.detail
    _order_logs_table().put_item(Item=item)
    if body.order_status_id == 3:
        _try_assign_courier(body.order_id)
    return {"ok": True, "order_id": body.order_id, "order_status_id": body.order_status_id}


@tracking.post("/update-courier-position")
def update_courier_position(body: UpdateCourierPositionIn) -> dict[str, Any]:
    ts = body.timestamp if body.timestamp is not None else int(time.time() * 1000)
    _courier_positions_table().put_item(
        Item={
            "courierId": body.courier_id,
            "timestamp": ts,
            "position": body.position,
            "lat": Decimal(str(body.lat)),
            "lon": Decimal(str(body.lon)),
        }
    )
    return {"ok": True, "courier_id": body.courier_id, "timestamp": ts}


@tracking.post("/update-courier-status")
def update_courier_status(body: UpdateCourierStatusIn) -> dict[str, Any]:
    conn_str = _db_conn_str()
    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    UPDATE couriers
                    SET status = %(status)s::courier_status_t
                    WHERE courier_id = %(courier_id)s
                    RETURNING courier_id
                    """,
                    {"status": body.status, "courier_id": body.courier_id},
                )
            except psycopg.errors.InvalidTextRepresentation as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail="invalid courier status",
                ) from exc
            row = cur.fetchone()
        conn.commit()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="courier not found")
    return {"ok": True, "courier_id": body.courier_id, "status": body.status}


@tracking.get("/get-courier-position")
def get_courier_position(courier_id: int = Query(...)) -> dict[str, Any]:
    resp = _courier_positions_table().query(
        KeyConditionExpression=Key("courierId").eq(courier_id),
        Limit=1,
        ScanIndexForward=False,
    )
    items = resp.get("Items", [])
    if not items:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="courier position not found")
    return _normalize_item(items[0])


@tracking.get("/get-order-status")
def get_order_status(order_id: int = Query(...)) -> dict[str, Any]:
    resp = _order_logs_table().query(
        KeyConditionExpression=Key("orderId").eq(order_id),
        Limit=1,
        ScanIndexForward=False,
    )
    items = resp.get("Items", [])
    if not items:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="order status not found")
    item = _normalize_item(items[0])
    return {
        "order_id": int(item["orderId"]),
        "timestamp": str(item["timestamp"]),
        "order_status_id": int(item["orderStatusId"]),
        "detail": item.get("detail"),
    }


@tracking.get("/get-order-log")
def get_order_log(order_id: int = Query(...)) -> dict[str, Any]:
    resp = _order_logs_table().query(
        KeyConditionExpression=Key("orderId").eq(order_id),
        ScanIndexForward=True,
    )
    items = [_normalize_item(i) for i in resp.get("Items", [])]
    return {"order_id": order_id, "items": items}


app.mount("/tracking", tracking)
