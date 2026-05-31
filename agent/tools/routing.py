"""Read-only routing service tools."""

from __future__ import annotations

from typing import Any

from agent.tools import http_client
from agent.tools.registry import ToolSpec, object_schema, register_tool


def _get_route_for_order(args: dict[str, Any]) -> dict[str, Any]:
    order_id = int(args["order_id"])
    status, body = http_client.get_json(
        http_client.routing_base_url(),
        "/routing/v1/get-route",
        params={"order_id": order_id},
    )
    result = http_client.normalize_http_result(status, body, tool="get_route_for_order")
    if status == 202 and result.get("ok"):
        result["status"] = "calculating"
    return result


def register_routing_tools() -> None:
    register_tool(
        ToolSpec(
            name="get_route_for_order",
            description=(
                "Cached delivery route for an order (geometry, distance, route_status). "
                "May return pending if route is still calculating."
            ),
            input_schema=object_schema(
                {"order_id": {"type": "integer"}},
                required=["order_id"],
            ),
            handler=_get_route_for_order,
            service="routing",
            status="beta",
            endpoint_ref="GET /routing/v1/get-route",
        )
    )
