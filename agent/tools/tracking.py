"""Read-only tracking service tools."""

from __future__ import annotations

from typing import Any

from agent.tools import http_client
from agent.tools.registry import ToolSpec, object_schema, register_tool


def _get_order_current_status(args: dict[str, Any]) -> dict[str, Any]:
    order_id = int(args["order_id"])
    status, body = http_client.get_json(
        http_client.tracking_base_url(),
        "/get-order-status",
        params={"order_id": order_id},
    )
    return http_client.normalize_http_result(status, body, tool="get_order_current_status")


def _get_order_history(args: dict[str, Any]) -> dict[str, Any]:
    order_id = int(args["order_id"])
    status, body = http_client.get_json(
        http_client.tracking_base_url(),
        "/get-order-log",
        params={"order_id": order_id},
    )
    return http_client.normalize_http_result(status, body, tool="get_order_history")


def _get_courier_position(args: dict[str, Any]) -> dict[str, Any]:
    courier_id = int(args["courier_id"])
    status, body = http_client.get_json(
        http_client.tracking_base_url(),
        "/get-courier-position",
        params={"courier_id": courier_id},
    )
    return http_client.normalize_http_result(status, body, tool="get_courier_position")


def register_tracking_tools() -> None:
    register_tool(
        ToolSpec(
            name="get_order_current_status",
            description="Latest order status event from the tracking log (timestamp, status id, detail).",
            input_schema=object_schema(
                {"order_id": {"type": "integer"}},
                required=["order_id"],
            ),
            handler=_get_order_current_status,
            service="tracking",
            status="stable",
            endpoint_ref="GET /tracking/get-order-status",
        )
    )
    register_tool(
        ToolSpec(
            name="get_order_history",
            description="Full chronological order status history from DynamoDB logs.",
            input_schema=object_schema(
                {"order_id": {"type": "integer"}},
                required=["order_id"],
            ),
            handler=_get_order_history,
            service="tracking",
            status="stable",
            endpoint_ref="GET /tracking/get-order-log",
        )
    )
    register_tool(
        ToolSpec(
            name="get_courier_position",
            description="Latest known courier GPS position.",
            input_schema=object_schema(
                {"courier_id": {"type": "integer"}},
                required=["courier_id"],
            ),
            handler=_get_courier_position,
            service="tracking",
            status="beta",
            endpoint_ref="GET /tracking/get-courier-position",
        )
    )
