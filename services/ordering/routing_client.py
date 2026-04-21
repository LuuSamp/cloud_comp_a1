"""HTTP client for the routing microservice (ALB paths under /routing)."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx


class RoutingClientError(Exception):
    """Routing service returned an error or unreachable."""


_CONNECT_TIMEOUT_S = float(os.environ.get("ROUTING_CONNECT_TIMEOUT_S", "5.0"))
_DEFAULT_TIMEOUT_S = float(os.environ.get("ROUTING_REQUEST_TIMEOUT_S", "30.0"))
_LIMITS = httpx.Limits(
    max_connections=int(os.environ.get("ROUTING_MAX_CONNECTIONS", "100")),
    max_keepalive_connections=int(
        os.environ.get("ROUTING_MAX_KEEPALIVE_CONNECTIONS", "20")
    ),
)
_CLIENT = httpx.Client(timeout=httpx.Timeout(_DEFAULT_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S), limits=_LIMITS)


def _post_json(path: str, body: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
    base = (os.environ.get("ROUTING_BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise RoutingClientError("ROUTING_BASE_URL is not set")
    url = f"{base}{path}"
    timeout = httpx.Timeout(timeout_s, connect=_CONNECT_TIMEOUT_S)
    try:
        r = _CLIENT.post(url, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        raise RoutingClientError(
            f"routing transport error ({type(exc).__name__})"
        ) from exc
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
    timeout_s: float = _DEFAULT_TIMEOUT_S,
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
    data = _post_json(
        "/routing/v1/nearest-courier?include_geometry=false",
        body,
        timeout_s=timeout_s,
    )
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
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    order_id: int | None = None,
    customer_id: int | None = None,
    food_place_id: int | None = None,
) -> float:
    """Return route distance in meters between origin and destination."""
    body = {
        "origin": {"lat": origin_lat, "lng": origin_lng},
        "destination": {"lat": destination_lat, "lng": destination_lng},
    }
    if order_id is not None:
        body["order_id"] = int(order_id)
    if customer_id is not None:
        body["customer_id"] = int(customer_id)
    if food_place_id is not None:
        body["food_place_id"] = int(food_place_id)
    data = _post_json(
        "/routing/v1/shortest-path?include_geometry=false",
        body,
        timeout_s=timeout_s,
    )
    err = data.get("error")
    if err:
        raise RoutingClientError(str(err))
    dist = data.get("distance_m")
    if dist is None:
        raise RoutingClientError("routing service returned no route distance")
    return float(dist)


def shortest_path_with_retry(
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    order_id: int | None = None,
    customer_id: int | None = None,
    food_place_id: int | None = None,
    attempts: int = 3,
    backoff_s: float = 0.5,
) -> float:
    last_exc: RoutingClientError | None = None
    for i in range(max(1, attempts)):
        try:
            return shortest_path(
                origin_lat,
                origin_lng,
                destination_lat,
                destination_lng,
                timeout_s=timeout_s,
                order_id=order_id,
                customer_id=customer_id,
                food_place_id=food_place_id,
            )
        except RoutingClientError as exc:
            last_exc = exc
            if i == max(1, attempts) - 1:
                break
            time.sleep(max(0.0, backoff_s) * (2**i))
    assert last_exc is not None
    raise last_exc


def shortest_path_payload(
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    order_id: int | None = None,
    customer_id: int | None = None,
    food_place_id: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "origin": {"lat": origin_lat, "lng": origin_lng},
        "destination": {"lat": destination_lat, "lng": destination_lng},
    }
    if order_id is not None:
        body["order_id"] = int(order_id)
    if customer_id is not None:
        body["customer_id"] = int(customer_id)
    if food_place_id is not None:
        body["food_place_id"] = int(food_place_id)
    data = _post_json("/routing/v1/shortest-path?include_geometry=true", body, timeout_s=timeout_s)
    if data.get("error"):
        raise RoutingClientError(str(data["error"]))
    if data.get("distance_m") is None:
        raise RoutingClientError("routing service returned no route distance")
    return data


def shortest_path_payload_with_retry(
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    order_id: int | None = None,
    customer_id: int | None = None,
    food_place_id: int | None = None,
    attempts: int = 3,
    backoff_s: float = 0.5,
) -> dict[str, Any]:
    last_exc: RoutingClientError | None = None
    for i in range(max(1, attempts)):
        try:
            return shortest_path_payload(
                origin_lat,
                origin_lng,
                destination_lat,
                destination_lng,
                timeout_s=timeout_s,
                order_id=order_id,
                customer_id=customer_id,
                food_place_id=food_place_id,
            )
        except RoutingClientError as exc:
            last_exc = exc
            if i == max(1, attempts) - 1:
                break
            time.sleep(max(0.0, backoff_s) * (2**i))
    assert last_exc is not None
    raise last_exc
