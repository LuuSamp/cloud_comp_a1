from __future__ import annotations

from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from dynamo import get_order_logs_table

router = APIRouter(prefix="/order-logs", tags=["order-logs"])


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in item.items():
        if isinstance(v, Decimal):
            out[k] = int(v) if v % 1 == 0 else float(v)
        else:
            out[k] = v
    return out


class OrderLogCreate(BaseModel):
    order_id: int
    timestamp: str = Field(
        ...,
        description="ISO-8601 string; part of composite key with order_id",
    )
    order_status_id: int = Field(..., ge=1, le=6, description="FK to RDS order_statuses")
    detail: str | None = None


class OrderLogUpdate(BaseModel):
    order_status_id: int = Field(..., ge=1, le=6)
    detail: str | None = None


class OrderLogOut(BaseModel):
    order_id: int
    timestamp: str
    order_status_id: int
    detail: str | None = None


def _item_to_out(item: dict[str, Any]) -> OrderLogOut:
    n = _normalize_item(item)
    return OrderLogOut(
        order_id=n["orderId"],
        timestamp=n["timestamp"],
        order_status_id=n["orderStatusId"],
        detail=n.get("detail"),
    )


def _composite_key(order_id: int, timestamp: str) -> dict[str, Any]:
    return {"orderId": order_id, "timestamp": timestamp}


@router.post("", response_model=OrderLogOut, status_code=status.HTTP_201_CREATED)
def create_order_log(body: OrderLogCreate) -> OrderLogOut:
    table = get_order_logs_table()
    item = {
        "orderId": body.order_id,
        "timestamp": body.timestamp,
        "orderStatusId": body.order_status_id,
    }
    if body.detail is not None:
        item["detail"] = body.detail
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(orderId) AND attribute_not_exists(#ts)",
            ExpressionAttributeNames={"#ts": "timestamp"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="Order log for this order_id and timestamp already exists",
            ) from e
        raise
    return OrderLogOut(
        order_id=body.order_id,
        timestamp=body.timestamp,
        order_status_id=body.order_status_id,
        detail=body.detail,
    )


@router.get("", response_model=list[OrderLogOut])
def list_order_logs(
    order_id: int | None = Query(default=None, description="Filter by order (table query)"),
    limit: int = Query(default=50, le=200),
) -> list[OrderLogOut]:
    table = get_order_logs_table()
    if order_id is not None:
        resp = table.query(
            KeyConditionExpression=Key("orderId").eq(order_id),
            Limit=limit,
            ScanIndexForward=True,
        )
        items = resp.get("Items", [])
    else:
        resp = table.scan(Limit=limit)
        items = resp.get("Items", [])
    return [_item_to_out(i) for i in items]


@router.get("/{order_id}/{timestamp}", response_model=OrderLogOut)
def get_order_log(order_id: int, timestamp: str) -> OrderLogOut:
    table = get_order_logs_table()
    resp = table.get_item(Key=_composite_key(order_id, timestamp))
    item = resp.get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Order log not found")
    return _item_to_out(item)


@router.put("/{order_id}/{timestamp}", response_model=OrderLogOut)
def replace_order_log(
    order_id: int, timestamp: str, body: OrderLogUpdate
) -> OrderLogOut:
    table = get_order_logs_table()
    key = _composite_key(order_id, timestamp)
    existing = table.get_item(Key=key).get("Item")
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Order log not found")
    item = {
        "orderId": order_id,
        "timestamp": timestamp,
        "orderStatusId": body.order_status_id,
    }
    if body.detail is not None:
        item["detail"] = body.detail
    table.put_item(Item=item)
    return OrderLogOut(
        order_id=order_id,
        timestamp=timestamp,
        order_status_id=body.order_status_id,
        detail=body.detail,
    )


@router.delete("/{order_id}/{timestamp}", status_code=status.HTTP_204_NO_CONTENT)
def delete_order_log(order_id: int, timestamp: str) -> Response:
    table = get_order_logs_table()
    resp = table.delete_item(
        Key=_composite_key(order_id, timestamp),
        ReturnValues="ALL_OLD",
    )
    if not resp.get("Attributes"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Order log not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
