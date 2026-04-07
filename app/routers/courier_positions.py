from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from dynamo import get_courier_positions_table

router = APIRouter(prefix="/courier-positions", tags=["courier-positions"])


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in item.items():
        if isinstance(v, Decimal):
            out[k] = int(v) if v % 1 == 0 else float(v)
        else:
            out[k] = v
    return out


class CourierPositionCreate(BaseModel):
    courier_id: int
    timestamp: int | None = Field(
        default=None,
        description="Epoch milliseconds; defaults to now if omitted",
    )
    position: str = Field(..., description="Text or JSON string")
    lat: float = Field(..., description="Latitude (WGS84), required")
    lon: float = Field(..., description="Longitude (WGS84), required")


class CourierPositionUpdate(BaseModel):
    position: str
    lat: float
    lon: float


class CourierPositionOut(BaseModel):
    courier_id: int
    timestamp: int
    position: str
    lat: float
    lon: float


def _item_to_out(item: dict[str, Any]) -> CourierPositionOut:
    n = _normalize_item(item)
    return CourierPositionOut(
        courier_id=n["courierId"],
        timestamp=n["timestamp"],
        position=n["position"],
        lat=float(n["lat"]),
        lon=float(n["lon"]),
    )


def _dec(v: float) -> Decimal:
    return Decimal(str(v))


@router.post("", response_model=CourierPositionOut, status_code=status.HTTP_201_CREATED)
def create_courier_position(body: CourierPositionCreate) -> CourierPositionOut:
    table = get_courier_positions_table()
    ts = body.timestamp if body.timestamp is not None else int(time.time() * 1000)
    item = {
        "courierId": body.courier_id,
        "timestamp": ts,
        "position": body.position,
        "lat": _dec(body.lat),
        "lon": _dec(body.lon),
    }
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(courierId) AND attribute_not_exists(#ts)",
            ExpressionAttributeNames={"#ts": "timestamp"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="Position with this courier_id and timestamp already exists",
            ) from e
        raise
    return CourierPositionOut(
        courier_id=body.courier_id,
        timestamp=ts,
        position=body.position,
        lat=body.lat,
        lon=body.lon,
    )


@router.get("", response_model=list[CourierPositionOut])
def list_courier_positions(
    courier_id: int | None = Query(default=None),
    limit: int = Query(default=50, le=200),
) -> list[CourierPositionOut]:
    table = get_courier_positions_table()
    if courier_id is not None:
        resp = table.query(
            KeyConditionExpression=Key("courierId").eq(courier_id),
            Limit=limit,
            ScanIndexForward=False,
        )
        items = resp.get("Items", [])
    else:
        resp = table.scan(Limit=limit)
        items = resp.get("Items", [])
    return [_item_to_out(i) for i in items]


@router.get("/{courier_id}/{timestamp_ms}", response_model=CourierPositionOut)
def get_courier_position(courier_id: int, timestamp_ms: int) -> CourierPositionOut:
    table = get_courier_positions_table()
    resp = table.get_item(Key={"courierId": courier_id, "timestamp": timestamp_ms})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Position not found")
    return _item_to_out(item)


@router.put("/{courier_id}/{timestamp_ms}", response_model=CourierPositionOut)
def update_courier_position(
    courier_id: int, timestamp_ms: int, body: CourierPositionUpdate
) -> CourierPositionOut:
    table = get_courier_positions_table()
    try:
        resp = table.update_item(
            Key={"courierId": courier_id, "timestamp": timestamp_ms},
            UpdateExpression="SET #p = :pos, #la = :lat, #lo = :lon",
            ExpressionAttributeNames={
                "#p": "position",
                "#la": "lat",
                "#lo": "lon",
                "#c": "courierId",
                "#ts": "timestamp",
            },
            ExpressionAttributeValues={
                ":pos": body.position,
                ":lat": _dec(body.lat),
                ":lon": _dec(body.lon),
            },
            ConditionExpression="attribute_exists(#c) AND attribute_exists(#ts)",
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail="Position not found"
            ) from e
        raise
    attrs = resp.get("Attributes")
    if not attrs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Position not found")
    return _item_to_out(attrs)


@router.delete("/{courier_id}/{timestamp_ms}", status_code=status.HTTP_204_NO_CONTENT)
def delete_courier_position(courier_id: int, timestamp_ms: int) -> Response:
    table = get_courier_positions_table()
    resp = table.delete_item(
        Key={"courierId": courier_id, "timestamp": timestamp_ms},
        ReturnValues="ALL_OLD",
    )
    if not resp.get("Attributes"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Position not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
