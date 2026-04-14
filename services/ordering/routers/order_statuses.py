from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from database import get_db

router = APIRouter(prefix="/order-statuses", tags=["order-statuses"])


class OrderStatusRow(BaseModel):
    order_status_id: int
    status: str


@router.get("", response_model=list[OrderStatusRow])
def list_order_statuses(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> list[OrderStatusRow]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT order_status_id, status
            FROM order_statuses
            ORDER BY order_status_id
            """
        )
        rows = cur.fetchall()
    return [OrderStatusRow(**r) for r in rows]
