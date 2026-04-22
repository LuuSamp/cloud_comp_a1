from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from database import get_db

router = APIRouter(prefix="/food-places", tags=["food-places"])

_BULK_MAX = 2000


class FoodPlaceCreate(BaseModel):
    name: str
    kitchen_type: str = Field(
        ...,
        description="PostgreSQL enum kitchen_type_t, e.g. UNSPECIFIED, OTHER",
    )
    address: str
    lat: float = Field(..., description="Latitude (WGS84), required")
    lon: float = Field(..., description="Longitude (WGS84), required")


class FoodPlaceUpdate(BaseModel):
    name: str
    kitchen_type: str
    address: str
    lat: float
    lon: float


class FoodPlaceOut(BaseModel):
    food_place_id: int
    name: str
    kitchen_type: str
    address: str
    lat: float
    lon: float


class BulkIds(BaseModel):
    ids: list[int] = Field(..., max_length=_BULK_MAX)


class BulkDeleteOut(BaseModel):
    deleted_ids: list[int]
    not_found_ids: list[int]


class FoodPlacesBulkIn(BaseModel):
    items: list[FoodPlaceCreate] = Field(..., max_length=_BULK_MAX)


class ClearTableOut(BaseModel):
    ok: bool = True
    orders_deleted: int
    table_truncated: str


@router.post("", response_model=FoodPlaceOut, status_code=status.HTTP_201_CREATED)
def create_food_place(
    body: FoodPlaceCreate,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> FoodPlaceOut:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO food_places (name, kitchen_type, address, lat, lon)
                VALUES (%(name)s, %(kitchen_type)s::kitchen_type_t, %(address)s, %(lat)s, %(lon)s)
                RETURNING food_place_id, name, kitchen_type::text, address, lat, lon
                """,
                body.model_dump(),
            )
        except psycopg.errors.InvalidTextRepresentation as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Invalid kitchen_type for enum kitchen_type_t",
            ) from e
        row = cur.fetchone()
    assert row is not None
    return FoodPlaceOut(**row)


@router.get("", response_model=list[FoodPlaceOut])
def list_food_places(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    skip: int = 0,
    limit: int = 50,
) -> list[FoodPlaceOut]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT food_place_id, name, kitchen_type::text AS kitchen_type, address, lat, lon
            FROM food_places
            ORDER BY food_place_id
            OFFSET %(skip)s LIMIT %(limit)s
            """,
            {"skip": skip, "limit": min(limit, 200)},
        )
        rows = cur.fetchall()
    return [FoodPlaceOut(**r) for r in rows]


@router.post("/bulk", response_model=list[FoodPlaceOut], status_code=status.HTTP_201_CREATED)
def bulk_create_food_places(
    body: FoodPlacesBulkIn,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> list[FoodPlaceOut]:
    if not body.items:
        return []
    fragments: list[str] = []
    params: dict[str, object] = {}
    for idx, item in enumerate(body.items):
        p = f"i{idx}_"
        fragments.append(
            f"(%({p}name)s, %({p}kitchen_type)s::kitchen_type_t, %({p}address)s, "
            f"%({p}lat)s, %({p}lon)s)"
        )
        d = item.model_dump()
        params[f"{p}name"] = d["name"]
        params[f"{p}kitchen_type"] = d["kitchen_type"]
        params[f"{p}address"] = d["address"]
        params[f"{p}lat"] = d["lat"]
        params[f"{p}lon"] = d["lon"]
    sql = (
        "INSERT INTO food_places (name, kitchen_type, address, lat, lon) VALUES "
        f"{', '.join(fragments)} "
        "RETURNING food_place_id, name, kitchen_type::text, address, lat, lon"
    )
    with conn.cursor() as cur:
        try:
            cur.execute(sql, params)
        except psycopg.errors.InvalidTextRepresentation as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Invalid kitchen_type for enum kitchen_type_t",
            ) from e
        rows = cur.fetchall()
    return [FoodPlaceOut(**r) for r in rows]


@router.post("/bulk-delete", response_model=BulkDeleteOut)
def bulk_delete_food_places(
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
                DELETE FROM food_places
                WHERE food_place_id = ANY(%(ids)s::int[])
                RETURNING food_place_id
                """,
                {"ids": ids},
            )
        except psycopg.errors.ForeignKeyViolation as e:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="One or more food places are still referenced by orders",
            ) from e
        deleted = [int(r["food_place_id"]) for r in cur.fetchall()]
    ds = set(deleted)
    not_found = sorted(i for i in ids if i not in ds)
    return BulkDeleteOut(deleted_ids=sorted(deleted), not_found_ids=not_found)


@router.post("/clear-all", response_model=ClearTableOut)
def clear_all_food_places(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> ClearTableOut:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM orders")
        n_orders = cur.rowcount if cur.rowcount is not None else 0
        cur.execute("TRUNCATE food_places RESTART IDENTITY")
    return ClearTableOut(orders_deleted=n_orders, table_truncated="food_places")


@router.get("/{food_place_id}", response_model=FoodPlaceOut)
def get_food_place(
    food_place_id: int,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> FoodPlaceOut:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT food_place_id, name, kitchen_type::text AS kitchen_type, address, lat, lon
            FROM food_places WHERE food_place_id = %(id)s
            """,
            {"id": food_place_id},
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Food place not found")
    return FoodPlaceOut(**row)


@router.put("/{food_place_id}", response_model=FoodPlaceOut)
def replace_food_place(
    food_place_id: int,
    body: FoodPlaceUpdate,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> FoodPlaceOut:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                UPDATE food_places SET
                    name = %(name)s,
                    kitchen_type = %(kitchen_type)s::kitchen_type_t,
                    address = %(address)s,
                    lat = %(lat)s,
                    lon = %(lon)s
                WHERE food_place_id = %(food_place_id)s
                RETURNING food_place_id, name, kitchen_type::text, address, lat, lon
                """,
                {**body.model_dump(), "food_place_id": food_place_id},
            )
        except psycopg.errors.InvalidTextRepresentation as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Invalid kitchen_type for enum kitchen_type_t",
            ) from e
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Food place not found")
    return FoodPlaceOut(**row)


@router.delete("/{food_place_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_food_place(
    food_place_id: int,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> Response:
    with conn.cursor() as cur:
        try:
            cur.execute(
                "DELETE FROM food_places WHERE food_place_id = %(id)s RETURNING food_place_id",
                {"id": food_place_id},
            )
        except psycopg.errors.ForeignKeyViolation as e:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="Food place is referenced by orders",
            ) from e
        row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Food place not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
