from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from database import get_db

router = APIRouter(prefix="/couriers", tags=["couriers"])


class CourierCreate(BaseModel):
    name: str
    vehicle_type: str = Field(..., description="enum vehicle_type_t")
    initial_address: str
    status: str = Field(default="idle", description="enum courier_status_t")
    last_position: str | None = None
    initial_lat: float = Field(..., description="Initial latitude (WGS84), required")
    initial_lon: float = Field(..., description="Initial longitude (WGS84), required")


class CourierUpdate(BaseModel):
    name: str
    vehicle_type: str
    initial_address: str
    status: str
    last_position: str | None = None
    initial_lat: float
    initial_lon: float


class CourierOut(BaseModel):
    courier_id: int
    name: str
    vehicle_type: str
    initial_address: str
    status: str
    last_position: str | None = None
    initial_lat: float
    initial_lon: float


@router.post("", response_model=CourierOut, status_code=status.HTTP_201_CREATED)
def create_courier(
    body: CourierCreate,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> CourierOut:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO couriers (
                    name, vehicle_type, initial_address, status,
                    last_position, initial_lat, initial_lon
                )
                VALUES (
                    %(name)s,
                    %(vehicle_type)s::vehicle_type_t,
                    %(initial_address)s,
                    %(status)s::courier_status_t,
                    %(last_position)s, %(initial_lat)s, %(initial_lon)s
                )
                RETURNING courier_id, name, vehicle_type::text, initial_address,
                          status::text, last_position, initial_lat, initial_lon
                """,
                body.model_dump(),
            )
        except psycopg.errors.InvalidTextRepresentation as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Invalid vehicle_type or status for PostgreSQL enums",
            ) from e
        row = cur.fetchone()
    assert row is not None
    return CourierOut(**row)


@router.get("", response_model=list[CourierOut])
def list_couriers(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    skip: int = 0,
    limit: int = 50,
) -> list[CourierOut]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT courier_id, name, vehicle_type::text, initial_address,
                   status::text, last_position, initial_lat, initial_lon
            FROM couriers
            ORDER BY courier_id
            OFFSET %(skip)s LIMIT %(limit)s
            """,
            {"skip": skip, "limit": min(limit, 200)},
        )
        rows = cur.fetchall()
    return [CourierOut(**r) for r in rows]


@router.get("/{courier_id}", response_model=CourierOut)
def get_courier(
    courier_id: int,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> CourierOut:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT courier_id, name, vehicle_type::text, initial_address,
                   status::text, last_position, initial_lat, initial_lon
            FROM couriers WHERE courier_id = %(id)s
            """,
            {"id": courier_id},
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Courier not found")
    return CourierOut(**row)


@router.put("/{courier_id}", response_model=CourierOut)
def replace_courier(
    courier_id: int,
    body: CourierUpdate,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> CourierOut:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                UPDATE couriers SET
                    name = %(name)s,
                    vehicle_type = %(vehicle_type)s::vehicle_type_t,
                    initial_address = %(initial_address)s,
                    status = %(status)s::courier_status_t,
                    last_position = %(last_position)s,
                    initial_lat = %(initial_lat)s,
                    initial_lon = %(initial_lon)s
                WHERE courier_id = %(courier_id)s
                RETURNING courier_id, name, vehicle_type::text, initial_address,
                          status::text, last_position, initial_lat, initial_lon
                """,
                {**body.model_dump(), "courier_id": courier_id},
            )
        except psycopg.errors.InvalidTextRepresentation as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Invalid vehicle_type or status for PostgreSQL enums",
            ) from e
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Courier not found")
    return CourierOut(**row)


@router.delete("/{courier_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_courier(
    courier_id: int,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> Response:
    with conn.cursor() as cur:
        try:
            cur.execute(
                "DELETE FROM couriers WHERE courier_id = %(id)s RETURNING courier_id",
                {"id": courier_id},
            )
        except psycopg.errors.ForeignKeyViolation as e:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="Courier is referenced by orders",
            ) from e
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Courier not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
