from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from database import get_db

router = APIRouter(prefix="/customers", tags=["customers"])

_BULK_MAX = 2000


class CustomerCreate(BaseModel):
    name: str
    email: str
    phone: str
    address: str
    lat: float = Field(..., description="Latitude (WGS84), required")
    lon: float = Field(..., description="Longitude (WGS84), required")


class CustomerUpdate(BaseModel):
    name: str
    email: str
    phone: str
    address: str
    lat: float
    lon: float


class CustomerOut(BaseModel):
    customer_id: int
    name: str
    email: str
    phone: str
    address: str
    lat: float
    lon: float


class BulkIds(BaseModel):
    ids: list[int] = Field(..., description="Customer IDs to delete", max_length=_BULK_MAX)


class BulkDeleteOut(BaseModel):
    deleted_ids: list[int]
    not_found_ids: list[int]


class CustomersBulkIn(BaseModel):
    items: list[CustomerCreate] = Field(..., max_length=_BULK_MAX)


class ClearTableOut(BaseModel):
    ok: bool = True
    orders_deleted: int
    table_truncated: str


@router.post("", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
def create_customer(
    body: CustomerCreate,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> CustomerOut:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO customers (name, email, phone, address, lat, lon)
                VALUES (%(name)s, %(email)s, %(phone)s, %(address)s, %(lat)s, %(lon)s)
                RETURNING customer_id, name, email, phone, address, lat, lon
                """,
                body.model_dump(),
            )
        except psycopg.errors.UniqueViolation as e:
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail="Email already registered"
            ) from e
        row = cur.fetchone()
    assert row is not None
    return CustomerOut(**row)


@router.get("", response_model=list[CustomerOut])
def list_customers(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    skip: int = 0,
    limit: int = 50,
) -> list[CustomerOut]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT customer_id, name, email, phone, address, lat, lon
            FROM customers
            ORDER BY customer_id
            OFFSET %(skip)s LIMIT %(limit)s
            """,
            {"skip": skip, "limit": min(limit, 200)},
        )
        rows = cur.fetchall()
    return [CustomerOut(**r) for r in rows]


@router.post("/bulk", response_model=list[CustomerOut], status_code=status.HTTP_201_CREATED)
def bulk_create_customers(
    body: CustomersBulkIn,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> list[CustomerOut]:
    if not body.items:
        return []
    cols = ("name", "email", "phone", "address", "lat", "lon")
    fragments: list[str] = []
    params: dict[str, object] = {}
    for idx, item in enumerate(body.items):
        p = f"i{idx}_"
        fragments.append(
            f"(%({p}name)s, %({p}email)s, %({p}phone)s, %({p}address)s, %({p}lat)s, %({p}lon)s)"
        )
        d = item.model_dump()
        for c in cols:
            params[f"{p}{c}"] = d[c]
    sql = (
        f"INSERT INTO customers ({', '.join(cols)}) VALUES {', '.join(fragments)} "
        "RETURNING customer_id, name, email, phone, address, lat, lon"
    )
    with conn.cursor() as cur:
        try:
            cur.execute(sql, params)
        except psycopg.errors.UniqueViolation as e:
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail="Duplicate email in bulk insert"
            ) from e
        rows = cur.fetchall()
    return [CustomerOut(**r) for r in rows]


@router.post("/bulk-delete", response_model=BulkDeleteOut)
def bulk_delete_customers(
    body: BulkIds,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> BulkDeleteOut:
    ids = list(dict.fromkeys(body.ids))
    if not ids:
        return BulkDeleteOut(deleted_ids=[], not_found_ids=[])
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                DELETE FROM customers
                WHERE customer_id = ANY(%(ids)s::int[])
                RETURNING customer_id
                """,
                {"ids": ids},
            )
        except psycopg.errors.ForeignKeyViolation as e:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="One or more customers are still referenced by orders",
            ) from e
        deleted = [int(r["customer_id"]) for r in cur.fetchall()]
    deleted_set = set(deleted)
    not_found = sorted(i for i in ids if i not in deleted_set)
    return BulkDeleteOut(deleted_ids=sorted(deleted), not_found_ids=not_found)


@router.post("/clear-all", response_model=ClearTableOut)
def clear_all_customers(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> ClearTableOut:
    """Delete all orders, then truncate customers (restarts identity)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM orders")
        n_orders = cur.rowcount if cur.rowcount is not None else 0
        cur.execute("TRUNCATE customers RESTART IDENTITY")
    return ClearTableOut(orders_deleted=n_orders, table_truncated="customers")


@router.get("/{customer_id}", response_model=CustomerOut)
def get_customer(
    customer_id: int,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> CustomerOut:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT customer_id, name, email, phone, address, lat, lon
            FROM customers WHERE customer_id = %(id)s
            """,
            {"id": customer_id},
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return CustomerOut(**row)


@router.put("/{customer_id}", response_model=CustomerOut)
def replace_customer(
    customer_id: int,
    body: CustomerUpdate,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> CustomerOut:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                UPDATE customers SET
                    name = %(name)s,
                    email = %(email)s,
                    phone = %(phone)s,
                    address = %(address)s,
                    lat = %(lat)s,
                    lon = %(lon)s
                WHERE customer_id = %(customer_id)s
                RETURNING customer_id, name, email, phone, address, lat, lon
                """,
                {**body.model_dump(), "customer_id": customer_id},
            )
        except psycopg.errors.UniqueViolation as e:
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail="Email already in use"
            ) from e
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return CustomerOut(**row)


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_customer(
    customer_id: int,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> Response:
    with conn.cursor() as cur:
        try:
            cur.execute(
                "DELETE FROM customers WHERE customer_id = %(id)s RETURNING customer_id",
                {"id": customer_id},
            )
        except psycopg.errors.ForeignKeyViolation as e:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="Customer is referenced by orders; delete or reassign orders first",
            ) from e
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
