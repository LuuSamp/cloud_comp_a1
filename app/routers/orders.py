from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from database import get_db

router = APIRouter(prefix="/orders", tags=["orders"])


class OrderCreate(BaseModel):
    customer_id: int
    food_place_id: int
    courier_id: int
    status: str = Field(default="CONFIRMED", description="Order lifecycle status (PDF)")


class OrderUpdate(BaseModel):
    customer_id: int
    food_place_id: int
    courier_id: int
    status: str


class OrderOut(BaseModel):
    order_id: int
    status: str
    customer_id: int
    food_place_id: int
    courier_id: int


@router.post("", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def create_order(
    body: OrderCreate,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> OrderOut:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO orders (status, customer_id, food_place_id, courier_id)
                VALUES (%(status)s, %(customer_id)s, %(food_place_id)s, %(courier_id)s)
                RETURNING order_id, status, customer_id, food_place_id, courier_id
                """,
                body.model_dump(),
            )
        except psycopg.errors.ForeignKeyViolation as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="customer_id, food_place_id, and/or courier_id does not exist",
            ) from e
        row = cur.fetchone()
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
            """
            SELECT order_id, status, customer_id, food_place_id, courier_id
            FROM orders
            ORDER BY order_id
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
        cur.execute(
            """
            SELECT order_id, status, customer_id, food_place_id, courier_id
            FROM orders WHERE order_id = %(id)s
            """,
            {"id": order_id},
        )
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
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                UPDATE orders SET
                    customer_id = %(customer_id)s,
                    food_place_id = %(food_place_id)s,
                    courier_id = %(courier_id)s,
                    status = %(status)s
                WHERE order_id = %(order_id)s
                RETURNING order_id, status, customer_id, food_place_id, courier_id
                """,
                {**body.model_dump(), "order_id": order_id},
            )
        except psycopg.errors.ForeignKeyViolation as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="customer_id, food_place_id, and/or courier_id does not exist",
            ) from e
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Order not found")
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
