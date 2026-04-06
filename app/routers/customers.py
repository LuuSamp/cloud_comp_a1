from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from database import get_db

router = APIRouter(prefix="/customers", tags=["customers"])


class CustomerCreate(BaseModel):
    name: str
    email: str
    phone: str
    address: str
    lat: float | None = None
    lng: float | None = None


class CustomerUpdate(BaseModel):
    name: str
    email: str
    phone: str
    address: str
    lat: float | None = None
    lng: float | None = None


class CustomerOut(BaseModel):
    customer_id: int
    name: str
    email: str
    phone: str
    address: str
    lat: float | None = None
    lng: float | None = None


@router.post("", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
def create_customer(
    body: CustomerCreate,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> CustomerOut:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO customers (name, email, phone, address, lat, lng)
                VALUES (%(name)s, %(email)s, %(phone)s, %(address)s, %(lat)s, %(lng)s)
                RETURNING customer_id, name, email, phone, address, lat, lng
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
            SELECT customer_id, name, email, phone, address, lat, lng
            FROM customers
            ORDER BY customer_id
            OFFSET %(skip)s LIMIT %(limit)s
            """,
            {"skip": skip, "limit": min(limit, 200)},
        )
        rows = cur.fetchall()
    return [CustomerOut(**r) for r in rows]


@router.get("/{customer_id}", response_model=CustomerOut)
def get_customer(
    customer_id: int,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> CustomerOut:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT customer_id, name, email, phone, address, lat, lng
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
                    lng = %(lng)s
                WHERE customer_id = %(customer_id)s
                RETURNING customer_id, name, email, phone, address, lat, lng
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
