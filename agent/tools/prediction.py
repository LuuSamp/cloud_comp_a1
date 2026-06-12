"""Predictive layer tools (delivery, demand, anomalies)."""

from __future__ import annotations

import os
from typing import Any

from agent.tools import http_client
from agent.tools.registry import ToolSpec, object_schema, register_tool


def _prediction_base_url() -> str:
    base = (os.environ.get("PREDICTION_BASE_URL") or os.environ.get("BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise ValueError("PREDICTION_BASE_URL or BASE_URL is not set")
    return base


def _get_delivery_prediction(args: dict[str, Any]) -> dict[str, Any]:
    order_id = int(args["order_id"])
    status, body = http_client.get_json(
        _prediction_base_url(),
        f"/prediction/v1/delivery-time/{order_id}",
    )
    return http_client.normalize_http_result(status, body, tool="get_delivery_prediction")


def _get_demand_forecast(args: dict[str, Any]) -> dict[str, Any]:
    _ = args
    status, body = http_client.get_json(
        _prediction_base_url(),
        "/prediction/v1/demand-forecast",
    )
    return http_client.normalize_http_result(status, body, tool="get_demand_forecast")


def _get_operational_anomalies(args: dict[str, Any]) -> dict[str, Any]:
    _ = args
    status, body = http_client.get_json(
        _prediction_base_url(),
        "/prediction/v1/anomalies",
    )
    return http_client.normalize_http_result(status, body, tool="get_operational_anomalies")


def register_prediction_tools() -> None:
    register_tool(
        ToolSpec(
            name="get_delivery_prediction",
            description="Get the predicted delivery time (seconds) for an order after place-order.",
            input_schema=object_schema(
                {"order_id": {"type": "integer", "description": "Order id"}},
                required=["order_id"],
            ),
            handler=_get_delivery_prediction,
            service="agent",
            status="beta",
            endpoint_ref="GET /prediction/v1/delivery-time/{order_id}",
        )
    )
    register_tool(
        ToolSpec(
            name="get_demand_forecast",
            description="Get batch demand forecasts by region grid, hour, and weekday.",
            input_schema=object_schema({}),
            handler=_get_demand_forecast,
            service="agent",
            status="beta",
            endpoint_ref="GET /prediction/v1/demand-forecast",
        )
    )
    register_tool(
        ToolSpec(
            name="get_operational_anomalies",
            description="Get latest operational anomaly scores from batch SageMaker inference.",
            input_schema=object_schema({}),
            handler=_get_operational_anomalies,
            service="agent",
            status="beta",
            endpoint_ref="GET /prediction/v1/anomalies",
        )
    )
