from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

import psycopg
from boto3.dynamodb.conditions import Key
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from database import get_db
from dynamo import get_courier_positions_table, get_order_logs_table
from routing_client import RoutingClientError, nearest_courier, shortest_path

router = APIRouter(tags=["workflow-orders"])


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


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


@router.post("/place-order", response_model=PlaceOrderOut, status_code=status.HTTP_201_CREATED)
def place_order(
    body: PlaceOrderIn,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> PlaceOrderOut:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO orders (order_status_id, customer_id, food_place_id, courier_id)
                VALUES (%(order_status_id)s, %(customer_id)s, %(food_place_id)s, NULL)
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

    try:
        shortest_path(
            float(food_place["lat"]),
            float(food_place["lon"]),
            float(customer["lat"]),
            float(customer["lon"]),
        )
    except RoutingClientError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=f"routing service: {exc}",
        ) from exc

    log_table = get_order_logs_table()
    log_table.put_item(
        Item={
            "orderId": order_id,
            "timestamp": _iso_now(),
            "orderStatusId": body.order_status_id,
            "detail": "order placed",
        }
    )
    return PlaceOrderOut(ok=True, message="route calculated", order_id=order_id)


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
