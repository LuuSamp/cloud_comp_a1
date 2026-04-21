"""Boto3 DynamoDB table handles (cached)."""

from __future__ import annotations

import os
from typing import Any

import boto3

_cached_ddb_resource: Any = None
_logs_table: Any = None
_positions_table: Any = None
_routes_table: Any = None


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get(
        "AWS_DEFAULT_REGION", "us-east-1"
    )


def _get_ddb_resource() -> Any:
    global _cached_ddb_resource
    if _cached_ddb_resource is None:
        _cached_ddb_resource = boto3.resource("dynamodb", region_name=_region())
    return _cached_ddb_resource


def get_order_logs_table() -> Any:
    global _logs_table
    if _logs_table is None:
        name = os.environ.get("DYNAMODB_ORDER_LOGS_TABLE", "")
        if not name:
            raise RuntimeError("DYNAMODB_ORDER_LOGS_TABLE is not set")
        _logs_table = _get_ddb_resource().Table(name)
    return _logs_table


def get_courier_positions_table() -> Any:
    global _positions_table
    if _positions_table is None:
        name = os.environ.get("DYNAMODB_COURIER_POSITIONS_TABLE", "")
        if not name:
            raise RuntimeError("DYNAMODB_COURIER_POSITIONS_TABLE is not set")
        _positions_table = _get_ddb_resource().Table(name)
    return _positions_table


def get_routes_table() -> Any:
    global _routes_table
    if _routes_table is None:
        name = os.environ.get("DYNAMODB_ROUTES_TABLE", "")
        if not name:
            raise RuntimeError("DYNAMODB_ROUTES_TABLE is not set")
        _routes_table = _get_ddb_resource().Table(name)
    return _routes_table
