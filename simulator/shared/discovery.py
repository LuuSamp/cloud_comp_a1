"""API discovery helpers for simulator services (no local state file)."""

from __future__ import annotations

from typing import Any

from simulator.shared.http_client import json_load, request_json


def _list_paginated(base_url: str, path: str, *, page_size: int = 200) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    skip = 0
    while True:
        code, raw = request_json(
            base_url,
            "GET",
            f"{path}?skip={skip}&limit={page_size}",
            timeout=60.0,
        )
        if code != 200:
            return out
        data = json_load(raw)
        if not isinstance(data, list):
            return out
        batch = [x for x in data if isinstance(x, dict)]
        out.extend(batch)
        if len(data) < page_size:
            return out
        skip += page_size


def list_customers(base_url: str) -> list[dict[str, Any]]:
    return _list_paginated(base_url, "/customers")


def list_food_places(base_url: str) -> list[dict[str, Any]]:
    return _list_paginated(base_url, "/food-places")


def list_couriers(base_url: str) -> list[dict[str, Any]]:
    return _list_paginated(base_url, "/couriers")


def list_orders(base_url: str) -> list[dict[str, Any]]:
    return _list_paginated(base_url, "/orders")
