from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from database import get_db

router = APIRouter(prefix="/couriers", tags=["couriers"])

_BULK_MAX = 2000


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


class BulkIds(BaseModel):
    ids: list[int] = Field(..., max_length=_BULK_MAX)


class BulkDeleteOut(BaseModel):
    deleted_ids: list[int]
    not_found_ids: list[int]


class CouriersBulkIn(BaseModel):
    items: list[CourierCreate] = Field(..., max_length=_BULK_MAX)


class ClearTableOut(BaseModel):
    ok: bool = True
    orders_courier_id_nulled: int
    table_truncated: str


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


@router.post("/bulk", response_model=list[CourierOut], status_code=status.HTTP_201_CREATED)
def bulk_create_couriers(
    body: CouriersBulkIn,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> list[CourierOut]:
    if not body.items:
        return []
    fragments: list[str] = []
    params: dict[str, object] = {}
    for idx, item in enumerate(body.items):
        p = f"i{idx}_"
        fragments.append(
            f"(%({p}name)s, %({p}vehicle_type)s::vehicle_type_t, %({p}initial_address)s, "
            f"%({p}status)s::courier_status_t, %({p}last_position)s, %({p}initial_lat)s, %({p}initial_lon)s)"
        )
        d = item.model_dump()
        params[f"{p}name"] = d["name"]
        params[f"{p}vehicle_type"] = d["vehicle_type"]
        params[f"{p}initial_address"] = d["initial_address"]
        params[f"{p}status"] = d["status"]
        params[f"{p}last_position"] = d["last_position"]
        params[f"{p}initial_lat"] = d["initial_lat"]
        params[f"{p}initial_lon"] = d["initial_lon"]
    sql = (
        """
        INSERT INTO couriers (
            name, vehicle_type, initial_address, status,
            last_position, initial_lat, initial_lon
        ) VALUES
        """
        + ", ".join(fragments)
        + """
        RETURNING courier_id, name, vehicle_type::text, initial_address,
                  status::text, last_position, initial_lat, initial_lon
        """
    )
    with conn.cursor() as cur:
        try:
            cur.execute(sql, params)
        except psycopg.errors.InvalidTextRepresentation as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Invalid vehicle_type or status for PostgreSQL enums",
            ) from e
        rows = cur.fetchall()
    return [CourierOut(**r) for r in rows]


@router.post("/bulk-delete", response_model=BulkDeleteOut)
def bulk_delete_couriers(
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
                DELETE FROM couriers
                WHERE courier_id = ANY(%(ids)s::int[])
                RETURNING courier_id
                """,
                {"ids": ids},
            )
        except psycopg.errors.ForeignKeyViolation as e:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="One or more couriers are still referenced by orders",
            ) from e
        deleted = [int(r["courier_id"]) for r in cur.fetchall()]
    ds = set(deleted)
    not_found = sorted(i for i in ids if i not in ds)
    return BulkDeleteOut(deleted_ids=sorted(deleted), not_found_ids=not_found)


@router.post("/clear-all", response_model=ClearTableOut)
def clear_all_couriers(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> ClearTableOut:
    """Null courier_id on all orders, then truncate couriers."""
    with conn.cursor() as cur:
        cur.execute("UPDATE orders SET courier_id = NULL WHERE courier_id IS NOT NULL")
        n = cur.rowcount if cur.rowcount is not None else 0
        cur.execute("TRUNCATE couriers RESTART IDENTITY")
    return ClearTableOut(orders_courier_id_nulled=n, table_truncated="couriers")


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
