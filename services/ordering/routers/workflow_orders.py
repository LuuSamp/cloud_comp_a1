from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from decimal import Decimal
from typing import Annotated, Any

import psycopg
from boto3.dynamodb.conditions import Key
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from database import get_db
from dynamo import get_courier_positions_table, get_order_logs_table, get_routes_table
from routing_client import (
    RoutingClientError,
    nearest_courier,
    shortest_path_payload_with_retry,
)

router = APIRouter(tags=["workflow-orders"])
log = logging.getLogger(__name__)


class PlaceOrderIn(BaseModel):
    customer_id: int
    food_place_id: int
    order_status_id: int = Field(default=1, ge=1, le=6)


class PlaceOrderOut(BaseModel):
    ok: bool
    message: str
    order_id: int


class AssignCourierIn(BaseModel):
    order_id: int


class AssignCourierOut(BaseModel):
    ok: bool
    order_id: int
    courier_id: int


class ClearOrdersAndLogsOut(BaseModel):
    ok: bool
    orders_deleted: int
    order_logs_deleted: int
    routes_deleted: int


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_dynamo_safe(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [_to_dynamo_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_dynamo_safe(v) for k, v in value.items()}
    return value


def _clear_dynamo_table_by_keys(table: Any, *, key_names: list[str]) -> int:
    deleted = 0
    last_key: dict[str, Any] | None = None
    expr_names = {f"#k{i}": name for i, name in enumerate(key_names)}
    projection = ", ".join(expr_names.keys())
    while True:
        scan_kwargs: dict[str, Any] = {
            "ProjectionExpression": projection,
            "ExpressionAttributeNames": expr_names,
        }
        if last_key is not None:
            scan_kwargs["ExclusiveStartKey"] = last_key
        resp = table.scan(**scan_kwargs)
        items = resp.get("Items", [])
        if items:
            with table.batch_writer() as batch:
                for item in items:
                    key = {k: item[k] for k in key_names if k in item}
                    if len(key) == len(key_names):
                        batch.delete_item(Key=key)
                        deleted += 1
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return deleted


def _latest_position_by_courier(courier_id: int) -> tuple[float, float] | None:
    """
    Latest lat/lon from Dynamo courier-positions (sort key = timestamp ms, descending).
    Skips older pages until an item with coordinates is found. None if no report exists.
    """
    table = get_courier_positions_table()
    last_key: dict[str, Any] | None = None
    while True:
        q: dict[str, Any] = {
            "KeyConditionExpression": Key("courierId").eq(courier_id),
            "Limit": 25,
            "ScanIndexForward": False,
        }
        if last_key is not None:
            q["ExclusiveStartKey"] = last_key
        resp = table.query(**q)
        for item in resp.get("Items", []):
            lat, lon = item.get("lat"), item.get("lon")
            if lat is not None and lon is not None:
                return float(lat), float(lon)
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return None


def _dispatch_route_calculation(
    *,
    order_id: int,
    customer_id: int,
    food_place_id: int,
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
) -> None:
    timeout_s = float(os.environ.get("PLACE_ORDER_ROUTING_TIMEOUT_S", "5.0"))
    attempts = int(os.environ.get("PLACE_ORDER_ROUTING_ATTEMPTS", "3"))
    backoff_s = float(os.environ.get("PLACE_ORDER_ROUTING_BACKOFF_S", "0.5"))

    def _db_conninfo() -> str:
        return (
            f"host={os.environ.get('DB_HOST', '')} "
            f"port={os.environ.get('DB_PORT', '5432')} "
            f"dbname={os.environ.get('DB_NAME', '')} "
            f"user={os.environ.get('DB_USER', '')} "
            f"password={os.environ.get('DB_PASSWORD', '')} "
            "connect_timeout=10"
        )

    def _update_route_status(route_status: str, route_error: str | None = None) -> None:
        with psycopg.connect(_db_conninfo()) as conn2:
            with conn2.cursor() as cur2:
                cur2.execute(
                    """
                    UPDATE orders
                    SET route_status = %(route_status)s,
                        route_error = %(route_error)s,
                        route_updated_at = NOW()
                    WHERE order_id = %(order_id)s
                    """,
                    {
                        "route_status": route_status,
                        "route_error": route_error,
                        "order_id": order_id,
                    },
                )
            conn2.commit()

    def _job() -> None:
        t0 = time.perf_counter()
        try:
            payload = shortest_path_payload_with_retry(
                origin_lat,
                origin_lng,
                destination_lat,
                destination_lng,
                timeout_s=timeout_s,
                order_id=order_id,
                customer_id=customer_id,
                food_place_id=food_place_id,
                attempts=attempts,
                backoff_s=backoff_s,
            )
            route_table = get_routes_table()
            distance_m = float(payload.get("distance_m") or 0.0)
            node_ids = [int(x) for x in (payload.get("node_ids") or [])]
            coordinates = payload.get("coordinates") or []
            now_iso = _iso_now()
            route_payload = {
                "distance_m": distance_m,
                "node_ids": node_ids,
                "coordinates": coordinates,
                "food_place_id": food_place_id,
                "customer_id": customer_id,
                "order_id": order_id,
                "updated_at": now_iso,
            }
            route_payload = _to_dynamo_safe(route_payload)
            route_table.put_item(
                Item={
                    "routeKey": f"order#{order_id}",
                    "payload": route_payload,
                    "updated_at": now_iso,
                }
            )
            route_table.put_item(
                Item={
                    "routeKey": f"pair#{food_place_id}#{customer_id}",
                    "payload": route_payload,
                    "updated_at": now_iso,
                }
            )
            _update_route_status("calculated", None)
            log.info(
                "async route ready order_id=%s elapsed_ms=%.1f attempts=%s",
                order_id,
                (time.perf_counter() - t0) * 1000.0,
                attempts,
            )
        except RoutingClientError as exc:
            _update_route_status("error", str(exc))
            log.warning(
                "async route failed order_id=%s elapsed_ms=%.1f attempts=%s error=%s",
                order_id,
                (time.perf_counter() - t0) * 1000.0,
                attempts,
                exc,
            )
        except Exception as exc:
            _update_route_status("error", f"{type(exc).__name__}: {exc}")
            log.exception("async route worker crashed order_id=%s", order_id)

    threading.Thread(
        target=_job,
        name=f"route-job-{order_id}",
        daemon=True,
    ).start()


@router.post("/place-order", response_model=PlaceOrderOut, status_code=status.HTTP_201_CREATED)
def place_order(
    body: PlaceOrderIn,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> PlaceOrderOut:
    if body.order_status_id >= 4:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="order_status_id >= 4 requires courier assignment",
        )
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO orders (order_status_id, customer_id, food_place_id, courier_id, route_status)
                VALUES (%(order_status_id)s, %(customer_id)s, %(food_place_id)s, NULL, 'calculating')
                RETURNING order_id
                """,
                body.model_dump(),
            )
        except psycopg.errors.ForeignKeyViolation as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="customer_id, food_place_id, and/or order_status_id invalid",
            ) from exc
        oid_row = cur.fetchone()
        assert oid_row is not None
        order_id = int(oid_row["order_id"])

        cur.execute(
            "SELECT lat, lon FROM customers WHERE customer_id = %(id)s",
            {"id": body.customer_id},
        )
        customer = cur.fetchone()
        cur.execute(
            "SELECT lat, lon FROM food_places WHERE food_place_id = %(id)s",
            {"id": body.food_place_id},
        )
        food_place = cur.fetchone()

    if not customer or not food_place:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="customer or food_place not found")

    _dispatch_route_calculation(
        order_id=order_id,
        customer_id=body.customer_id,
        food_place_id=body.food_place_id,
        origin_lat=float(food_place["lat"]),
        origin_lng=float(food_place["lon"]),
        destination_lat=float(customer["lat"]),
        destination_lng=float(customer["lon"]),
    )

    log_table = get_order_logs_table()
    log_table.put_item(
        Item={
            "orderId": order_id,
            "timestamp": _iso_now(),
            "orderStatusId": body.order_status_id,
            "detail": "order placed",
        }
    )
    return PlaceOrderOut(
        ok=True,
        message="order placed; route calculation queued",
        order_id=order_id,
    )


@router.post("/admin/clear-orders-and-logs", response_model=ClearOrdersAndLogsOut)
def clear_orders_and_logs(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> ClearOrdersAndLogsOut:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM orders")
            row = cur.fetchone()
            orders_deleted = int((row or {}).get("n") or 0)
            cur.execute("DELETE FROM orders")
            cur.execute("SELECT pg_get_serial_sequence('orders', 'order_id') AS seq")
            seq_row = cur.fetchone()
            seq_name = (seq_row or {}).get("seq")
            if seq_name:
                cur.execute(
                    "SELECT setval(CAST(%(seq)s AS regclass), 1, false)",
                    {"seq": seq_name},
                )
    except Exception as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to clear orders in RDS: {type(exc).__name__}: {exc}",
        ) from exc

    try:
        order_logs_deleted = _clear_dynamo_table_by_keys(
            get_order_logs_table(),
            key_names=["orderId", "timestamp"],
        )
    except Exception as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to clear order logs in DynamoDB: {type(exc).__name__}: {exc}",
        ) from exc

    routes_deleted = 0
    try:
        routes_deleted = _clear_dynamo_table_by_keys(get_routes_table(), key_names=["routeKey"])
    except RuntimeError:
        routes_deleted = 0
    except Exception as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to clear routes in DynamoDB: {type(exc).__name__}: {exc}",
        ) from exc

    return ClearOrdersAndLogsOut(
        ok=True,
        orders_deleted=orders_deleted,
        order_logs_deleted=order_logs_deleted,
        routes_deleted=routes_deleted,
    )


@router.post("/assign-courier", response_model=AssignCourierOut)
def assign_courier(
    body: AssignCourierIn,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> AssignCourierOut:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT order_id, order_status_id, food_place_id, courier_id
            FROM orders
            WHERE order_id = %(order_id)s
            """,
            {"order_id": body.order_id},
        )
        order = cur.fetchone()
    if not order:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="order not found")
    if int(order["order_status_id"]) != 3:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="order must be READY_FOR_PICKUP to assign courier",
        )
    existing_courier = order["courier_id"]
    if existing_courier is not None:
        return AssignCourierOut(ok=True, order_id=body.order_id, courier_id=int(existing_courier))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT lat, lon FROM food_places WHERE food_place_id = %(id)s",
            {"id": int(order["food_place_id"])},
        )
        restaurant = cur.fetchone()
        cur.execute(
            """
            SELECT courier_id, initial_lat, initial_lon
            FROM couriers
            WHERE status = 'available'
            """,
        )
        couriers = cur.fetchall()
    if not restaurant:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="food_place not found")
    if not couriers:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="no available couriers")

    # Nearest-courier uses latest Dynamo position when present; else initial_lat/lon from RDS.
    candidates: list[tuple[int, float, float]] = []
    for row in couriers:
        cid = int(row["courier_id"])
        latest = _latest_position_by_courier(cid)
        if latest is not None:
            lat, lon = latest
        else:
            lat = float(row["initial_lat"])
            lon = float(row["initial_lon"])
        candidates.append((cid, lat, lon))

    try:
        courier_id, _dist = nearest_courier(
            float(restaurant["lat"]),
            float(restaurant["lon"]),
            candidates,
        )
    except RoutingClientError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=f"routing service: {exc}",
        ) from exc

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE orders
            SET courier_id = %(courier_id)s
            WHERE order_id = %(order_id)s AND courier_id IS NULL
            RETURNING order_id
            """,
            {"order_id": body.order_id, "courier_id": courier_id},
        )
        updated = cur.fetchone()
    if not updated:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="order already assigned by another request",
        )
    return AssignCourierOut(ok=True, order_id=body.order_id, courier_id=courier_id)
