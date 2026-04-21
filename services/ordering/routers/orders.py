from __future__ import annotations

import os
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from database import get_db
from routing_client import RoutingClientError, nearest_courier

router = APIRouter(prefix="/orders", tags=["orders"])


def _validate_status_courier_pair(order_status_id: int, courier_id: int | None) -> None:
    if order_status_id >= 4 and courier_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="order_status_id >= 4 requires courier_id",
        )


class OrderCreate(BaseModel):
    customer_id: int
    food_place_id: int
    courier_id: int | None = None
    order_status_id: int = Field(
        default=1,
        ge=1,
        le=6,
        description="FK to order_statuses (1=CONFIRMED … 6=DELIVERED)",
    )


class OrderUpdate(BaseModel):
    customer_id: int
    food_place_id: int
    courier_id: int
    order_status_id: int = Field(..., ge=1, le=6)


class OrderOut(BaseModel):
    order_id: int
    order_status_id: int
    order_status: str
    customer_id: int
    food_place_id: int
    courier_id: int | None
    route_status: str | None = None
    route_error: str | None = None


_SELECT_ORDER = """
    SELECT o.order_id, o.order_status_id, s.status AS order_status,
           o.customer_id, o.food_place_id, o.courier_id, o.route_status, o.route_error
    FROM orders o
    JOIN order_statuses s ON s.order_status_id = o.order_status_id
"""


@router.post("", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def create_order(
    body: OrderCreate,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> OrderOut:
    _validate_status_courier_pair(body.order_status_id, body.courier_id)
    courier_id = body.courier_id
    if courier_id is None:
        base = (os.environ.get("ROUTING_BASE_URL") or "").strip()
        if not base:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="courier_id is required when ROUTING_BASE_URL is not configured",
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lat, lon FROM customers WHERE customer_id = %(id)s",
                {"id": body.customer_id},
            )
            crow = cur.fetchone()
            cur.execute(
                "SELECT lat, lon FROM food_places WHERE food_place_id = %(id)s",
                {"id": body.food_place_id},
            )
            frow = cur.fetchone()
            cur.execute(
                """
                SELECT courier_id, initial_lat, initial_lon FROM couriers
                WHERE status = 'available'
                """,
            )
            available = cur.fetchall()
        if not crow or not frow:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="customer or restaurant not found",
            )
        if not available:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="no available couriers for assignment",
            )
        candidates = [
            (r["courier_id"], float(r["initial_lat"]), float(r["initial_lon"]))
            for r in available
        ]
        try:
            courier_id, _dist = nearest_courier(
                float(frow["lat"]),
                float(frow["lon"]),
                candidates,
            )
        except RoutingClientError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail=f"routing service: {exc}",
            ) from exc

    payload = {
        "order_status_id": body.order_status_id,
        "customer_id": body.customer_id,
        "food_place_id": body.food_place_id,
        "courier_id": courier_id,
    }
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"""
                INSERT INTO orders (order_status_id, customer_id, food_place_id, courier_id)
                VALUES (%(order_status_id)s, %(customer_id)s, %(food_place_id)s, %(courier_id)s)
                RETURNING order_id
                """,
                payload,
            )
            oid_row = cur.fetchone()
            assert oid_row is not None
            cur.execute(
                _SELECT_ORDER + " WHERE o.order_id = %(id)s",
                {"id": oid_row["order_id"]},
            )
            row = cur.fetchone()
        except psycopg.errors.ForeignKeyViolation as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="customer_id, food_place_id, courier_id, and/or order_status_id invalid",
            ) from e
    assert row is not None
    return OrderOut(**row)


@router.get("", response_model=list[OrderOut])
def list_orders(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    skip: int = 0,
    limit: int = 50,
) -> list[OrderOut]:
    with conn.cursor() as cur:
        cur.execute(
            _SELECT_ORDER
            + """
            ORDER BY o.order_id
            OFFSET %(skip)s LIMIT %(limit)s
            """,
            {"skip": skip, "limit": min(limit, 200)},
        )
        rows = cur.fetchall()
    return [OrderOut(**r) for r in rows]


@router.get("/{order_id}", response_model=OrderOut)
def get_order(
    order_id: int,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> OrderOut:
    with conn.cursor() as cur:
        cur.execute(_SELECT_ORDER + " WHERE o.order_id = %(id)s", {"id": order_id})
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Order not found")
    return OrderOut(**row)


@router.put("/{order_id}", response_model=OrderOut)
def replace_order(
    order_id: int,
    body: OrderUpdate,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> OrderOut:
    _validate_status_courier_pair(body.order_status_id, body.courier_id)
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                UPDATE orders SET
                    customer_id = %(customer_id)s,
                    food_place_id = %(food_place_id)s,
                    courier_id = %(courier_id)s,
                    order_status_id = %(order_status_id)s
                WHERE order_id = %(order_id)s
                RETURNING order_id
                """,
                {**body.model_dump(), "order_id": order_id},
            )
        except psycopg.errors.ForeignKeyViolation as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="customer_id, food_place_id, courier_id, and/or order_status_id invalid",
            ) from e
        updated = cur.fetchone()
        if not updated:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Order not found")
        cur.execute(_SELECT_ORDER + " WHERE o.order_id = %(id)s", {"id": order_id})
        row = cur.fetchone()
    assert row is not None
    return OrderOut(**row)


@router.delete("/{order_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_order(
    order_id: int,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> Response:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM orders WHERE order_id = %(id)s RETURNING order_id",
            {"id": order_id},
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Order not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
