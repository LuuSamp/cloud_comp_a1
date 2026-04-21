"""DijkFood tracking service. ALB path prefix: /tracking"""

from __future__ import annotations

import datetime as dt
import logging
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
log = logging.getLogger(__name__)
_STATUS_OUTCOMES: dict[str, int] = {}


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


def _row_get(row: Any, key: str, index: int) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[index]
    except Exception:
        return None


def _ordering_base_url() -> str | None:
    for key in ("ORDERING_BASE_URL", "BASE_URL", "ROUTING_BASE_URL"):
        value = (os.environ.get(key) or "").strip().rstrip("/")
        if value:
            return value
    return None


def _note_status_outcome(key: str) -> None:
    _STATUS_OUTCOMES[key] = _STATUS_OUTCOMES.get(key, 0) + 1


def _try_assign_courier(order_id: int, *, max_elapsed_s: float | None = None) -> None:
    base = _ordering_base_url()
    if not base:
        log.warning("assign-courier skipped order_id=%s reason=no_ordering_base_url", order_id)
        return
    attempts = max(1, int((os.environ.get("ASSIGN_COURIER_ATTEMPTS") or "5").strip()))
    backoff_s = max(
        0.0, float((os.environ.get("ASSIGN_COURIER_BACKOFF_S") or "0.5").strip())
    )
    timeout = httpx.Timeout(
        float((os.environ.get("ASSIGN_COURIER_TIMEOUT_S") or "20").strip()),
        connect=5.0,
    )
    deadline = time.monotonic() + max_elapsed_s if max_elapsed_s is not None else None
    with httpx.Client(timeout=timeout) as client:
        for i in range(attempts):
            if deadline is not None and time.monotonic() >= deadline:
                log.info(
                    "assign-courier budget exceeded order_id=%s budget_s=%.2f",
                    order_id,
                    max_elapsed_s,
                )
                return
            try:
                resp = client.post(f"{base}/assign-courier", json={"order_id": order_id})
            except httpx.HTTPError as exc:
                if i == attempts - 1:
                    log.warning(
                        "assign-courier failed order_id=%s attempts=%s error=%s",
                        order_id,
                        attempts,
                        type(exc).__name__,
                    )
                    return
                time.sleep(backoff_s * (2**i))
                continue
            if resp.status_code in (200, 201):
                log.info(
                    "assign-courier success order_id=%s attempt=%s",
                    order_id,
                    i + 1,
                )
                return
            if resp.status_code in (404,):
                log.warning(
                    "assign-courier stop order_id=%s status=%s",
                    order_id,
                    resp.status_code,
                )
                return
            if i == attempts - 1:
                log.warning(
                    "assign-courier failed order_id=%s attempts=%s status=%s body=%s",
                    order_id,
                    attempts,
                    resp.status_code,
                    (resp.text or "")[:200],
                )
                return
            time.sleep(backoff_s * (2**i))


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
    t0 = time.perf_counter()
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
                WHERE order_id = %(order_id)s
                  AND order_status_id = %(prev)s
                  AND (%(next)s < 4 OR courier_id IS NOT NULL)
                RETURNING order_id
                """,
                {"order_id": body.order_id, "prev": prev, "next": body.order_status_id},
            )
            updated = cur.fetchone()
            if not updated and body.order_status_id >= 4:
                cur.execute(
                    """
                    SELECT courier_id, order_status_id
                    FROM orders
                    WHERE order_id = %(order_id)s
                    """,
                    {"order_id": body.order_id},
                )
                row = cur.fetchone()
                courier_id = _row_get(row, "courier_id", 0)
                order_status_id = _row_get(row, "order_status_id", 1)
                if (
                    row
                    and courier_id is None
                    and order_status_id is not None
                    and int(order_status_id) == prev
                ):
                    log.warning(
                        "blocked status transition order_id=%s prev=%s next=%s reason=no_courier",
                        body.order_id,
                        prev,
                        body.order_status_id,
                    )
                    _note_status_outcome("conflict_no_courier")
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        detail={
                            "code": "conflict_no_courier",
                            "message": "transition rejected: courier must be assigned before PICKED_UP",
                            "retryable": True,
                            "retry_after_s": 1.0,
                        },
                    )
        conn.commit()
    if not updated:
        _note_status_outcome("conflict_prior_status")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "conflict_prior_status",
                "message": "transition rejected: order is not at prior status",
                "retryable": True,
                "retry_after_s": 0.5,
            },
        )

    item = {
        "orderId": body.order_id,
        "timestamp": _iso_now(),
        "orderStatusId": body.order_status_id,
    }
    if body.detail:
        item["detail"] = body.detail
    _order_logs_table().put_item(Item=item)
    if body.order_status_id >= 3:
        # Keep request latency bounded; reconcile endpoint handles eventual assignment.
        req_budget_s = max(
            0.2,
            float((os.environ.get("ASSIGN_COURIER_REQUEST_BUDGET_S") or "3.0").strip()),
        )
        _try_assign_courier(body.order_id, max_elapsed_s=req_budget_s)
    _note_status_outcome("ok")
    log.info(
        "update-order-status ok order_id=%s next=%s elapsed_ms=%.1f outcomes=%s",
        body.order_id,
        body.order_status_id,
        (time.perf_counter() - t0) * 1000.0,
        _STATUS_OUTCOMES,
    )
    return {"ok": True, "order_id": body.order_id, "order_status_id": body.order_status_id}


@tracking.post("/reconcile-courier-assignments")
def reconcile_courier_assignments(limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    conn_str = _db_conn_str()
    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT order_id
                FROM orders
                WHERE order_status_id >= 3
                  AND order_status_id < 6
                  AND courier_id IS NULL
                ORDER BY order_id
                LIMIT %(limit)s
                """,
                {"limit": int(limit)},
            )
            rows = cur.fetchall()
    ids = []
    for r in rows:
        oid = _row_get(r, "order_id", 0)
        if oid is None:
            continue
        ids.append(int(oid))
    for oid in ids:
        _try_assign_courier(oid)
    return {"ok": True, "reconciled": len(ids), "order_ids": ids}


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
