from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from database import get_db

router = APIRouter(prefix="/food-places", tags=["food-places"])


class FoodPlaceCreate(BaseModel):
    name: str
    kitchen_type: str = Field(
        ...,
        description="PostgreSQL enum kitchen_type_t, e.g. UNSPECIFIED, OTHER",
    )
    address: str
    lat: float | None = None
    lng: float | None = None


class FoodPlaceUpdate(BaseModel):
    name: str
    kitchen_type: str
    address: str
    lat: float | None = None
    lng: float | None = None


class FoodPlaceOut(BaseModel):
    food_place_id: int
    name: str
    kitchen_type: str
    address: str
    lat: float | None = None
    lng: float | None = None


@router.post("", response_model=FoodPlaceOut, status_code=status.HTTP_201_CREATED)
def create_food_place(
    body: FoodPlaceCreate,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> FoodPlaceOut:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO food_places (name, kitchen_type, address, lat, lng)
                VALUES (%(name)s, %(kitchen_type)s::kitchen_type_t, %(address)s, %(lat)s, %(lng)s)
                RETURNING food_place_id, name, kitchen_type::text, address, lat, lng
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
            SELECT food_place_id, name, kitchen_type::text AS kitchen_type, address, lat, lng
            FROM food_places
            ORDER BY food_place_id
            OFFSET %(skip)s LIMIT %(limit)s
            """,
            {"skip": skip, "limit": min(limit, 200)},
        )
        rows = cur.fetchall()
    return [FoodPlaceOut(**r) for r in rows]


@router.get("/{food_place_id}", response_model=FoodPlaceOut)
def get_food_place(
    food_place_id: int,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> FoodPlaceOut:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT food_place_id, name, kitchen_type::text AS kitchen_type, address, lat, lng
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
                    lng = %(lng)s
                WHERE food_place_id = %(food_place_id)s
                RETURNING food_place_id, name, kitchen_type::text, address, lat, lng
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
