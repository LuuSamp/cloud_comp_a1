from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from dynamo import get_order_logs_table

router = APIRouter(prefix="/order-logs", tags=["order-logs"])

GSI_NAME = "orderId-timestamp-index"


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in item.items():
        if isinstance(v, Decimal):
            out[k] = int(v) if v % 1 == 0 else float(v)
        else:
            out[k] = v
    return out


class OrderLogCreate(BaseModel):
    order_log_id: str | None = Field(
        default=None,
        description="If omitted, a UUID is generated",
    )
    order_id: int
    timestamp: str = Field(
        ...,
        description="ISO-8601 string; used as GSI sort key",
    )
    status: str | None = None
    detail: str | None = None


class OrderLogUpdate(BaseModel):
    order_id: int
    timestamp: str
    status: str | None = None
    detail: str | None = None


class OrderLogOut(BaseModel):
    order_log_id: str
    order_id: int
    timestamp: str
    status: str | None = None
    detail: str | None = None


def _item_to_out(item: dict[str, Any]) -> OrderLogOut:
    n = _normalize_item(item)
    return OrderLogOut(
        order_log_id=n["orderLogId"],
        order_id=n["orderId"],
        timestamp=n["timestamp"],
        status=n.get("status"),
        detail=n.get("detail"),
    )


@router.post("", response_model=OrderLogOut, status_code=status.HTTP_201_CREATED)
def create_order_log(body: OrderLogCreate) -> OrderLogOut:
    table = get_order_logs_table()
    oid = body.order_log_id or str(uuid4())
    item = {
        "orderLogId": oid,
        "orderId": body.order_id,
        "timestamp": body.timestamp,
    }
    if body.status is not None:
        item["status"] = body.status
    if body.detail is not None:
        item["detail"] = body.detail
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(orderLogId)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail="order_log_id already exists"
            ) from e
        raise
    return OrderLogOut(
        order_log_id=oid,
        order_id=body.order_id,
        timestamp=body.timestamp,
        status=body.status,
        detail=body.detail,
    )


@router.get("", response_model=list[OrderLogOut])
def list_order_logs(
    order_id: int | None = Query(default=None, description="Filter by order (GSI query)"),
    limit: int = Query(default=50, le=200),
) -> list[OrderLogOut]:
    table = get_order_logs_table()
    if order_id is not None:
        resp = table.query(
            IndexName=GSI_NAME,
            KeyConditionExpression=Key("orderId").eq(order_id),
            Limit=limit,
            ScanIndexForward=True,
        )
        items = resp.get("Items", [])
    else:
        resp = table.scan(Limit=limit)
        items = resp.get("Items", [])
    return [_item_to_out(i) for i in items]


@router.get("/{order_log_id}", response_model=OrderLogOut)
def get_order_log(order_log_id: str) -> OrderLogOut:
    table = get_order_logs_table()
    resp = table.get_item(Key={"orderLogId": order_log_id})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Order log not found")
    return _item_to_out(item)


@router.put("/{order_log_id}", response_model=OrderLogOut)
def replace_order_log(order_log_id: str, body: OrderLogUpdate) -> OrderLogOut:
    table = get_order_logs_table()
    existing = table.get_item(Key={"orderLogId": order_log_id}).get("Item")
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Order log not found")
    table.delete_item(Key={"orderLogId": order_log_id})
    item = {
        "orderLogId": order_log_id,
        "orderId": body.order_id,
        "timestamp": body.timestamp,
    }
    if body.status is not None:
        item["status"] = body.status
    if body.detail is not None:
        item["detail"] = body.detail
    table.put_item(Item=item)
    return OrderLogOut(
        order_log_id=order_log_id,
        order_id=body.order_id,
        timestamp=body.timestamp,
        status=body.status,
        detail=body.detail,
    )


@router.delete("/{order_log_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_order_log(order_log_id: str) -> Response:
    table = get_order_logs_table()
    resp = table.delete_item(
        Key={"orderLogId": order_log_id},
        ReturnValues="ALL_OLD",
    )
    if not resp.get("Attributes"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Order log not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
