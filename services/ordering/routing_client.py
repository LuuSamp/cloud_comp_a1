"""HTTP client for the routing microservice (ALB paths under /routing)."""

from __future__ import annotations

import os
from typing import Any

import httpx


class RoutingClientError(Exception):
    """Routing service returned an error or unreachable."""


def _post_json(path: str, body: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
    base = (os.environ.get("ROUTING_BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise RoutingClientError("ROUTING_BASE_URL is not set")
    url = f"{base}{path}"
    timeout = httpx.Timeout(timeout_s, connect=10.0)
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=body)
    if r.status_code == 503:
        raise RoutingClientError("routing service unavailable (graph loading or overload)")
    if r.status_code >= 400:
        raise RoutingClientError(r.text or f"HTTP {r.status_code}")
    data = r.json()
    if not isinstance(data, dict):
        raise RoutingClientError("invalid routing response")
    return data


def nearest_courier(
    restaurant_lat: float,
    restaurant_lng: float,
    candidates: list[tuple[int, float, float]],
    *,
    timeout_s: float = 120.0,
) -> tuple[int, float]:
    """
    Pick courier minimizing drive distance from restaurant (routing service).
    candidates: (courier_id, lat, lng)
    """
    if not candidates:
        raise RoutingClientError("no courier candidates")
    body = {
        "restaurant": {"lat": restaurant_lat, "lng": restaurant_lng},
        "candidates": [
            {"courier_id": cid, "lat": lat, "lng": lng}
            for cid, lat, lng in candidates
        ],
    }
    data = _post_json("/routing/v1/nearest-courier", body, timeout_s=timeout_s)
    err = data.get("error")
    if err:
        raise RoutingClientError(str(err))
    cid = data.get("courier_id")
    dist = data.get("distance_m")
    if cid is None or dist is None:
        raise RoutingClientError("routing service returned no assignment")
    return int(cid), float(dist)


def shortest_path(
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
    *,
    timeout_s: float = 120.0,
) -> float:
    """Return route distance in meters between origin and destination."""
    body = {
        "origin": {"lat": origin_lat, "lng": origin_lng},
        "destination": {"lat": destination_lat, "lng": destination_lng},
    }
    data = _post_json("/routing/v1/shortest-path", body, timeout_s=timeout_s)
    err = data.get("error")
    if err:
        raise RoutingClientError(str(err))
    dist = data.get("distance_m")
    if dist is None:
        raise RoutingClientError("routing service returned no route distance")
    return float(dist)
