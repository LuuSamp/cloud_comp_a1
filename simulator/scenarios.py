"""
Load-test scenario steps: place order, courier position, order lifecycle + log + verify.
"""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Any

from .http_client import json_load, request_json


class LoadContext:
    """HTTP + metrics for one load-test run."""

    def __init__(
        self,
        base_url: str,
        routing_base_url: str | None,
        latencies: list[float],
        errors: list[str],
        lock: threading.Lock,
        debug_http: bool,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.routing_base_url = (routing_base_url or "").strip() or None
        self.latencies = latencies
        self.errors = errors
        self.lock = lock
        self.debug_http = debug_http

    def req(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 60.0,
    ) -> tuple[int, bytes]:
        def on_lat(d: float) -> None:
            with self.lock:
                self.latencies.append(d)

        def on_err(m: str) -> None:
            with self.lock:
                self.errors.append(m)

        return request_json(
            self.base_url,
            method,
            path,
            body=body,
            timeout=timeout,
            on_latency=on_lat,
            on_error=on_err,
            debug_http=self.debug_http,
        )


def simulate_user_order(
    ctx: LoadContext,
    customer_id: int,
    food_place_id: int,
    courier_id_fallback: int,
) -> int | None:
    """
    Try POST /sim/orders/place; on 404/501 fall back to POST /orders.
    Omits courier_id when ROUTING_BASE_URL is configured (same as ordering ECS).
    """
    code, raw = ctx.req(
        "POST",
        "/sim/orders/place",
        {"customer_id": customer_id, "food_place_id": food_place_id},
    )
    if code == 201:
        data = json_load(raw)
        if isinstance(data, dict) and "order_id" in data:
            return int(data["order_id"])
    if code not in (404, 501):
        return None

    body: dict[str, Any] = {
        "customer_id": customer_id,
        "food_place_id": food_place_id,
        "order_status_id": 1,
    }
    if not ctx.routing_base_url:
        body["courier_id"] = courier_id_fallback
    code2, raw2 = ctx.req("POST", "/orders", body)
    if code2 == 201:
        return int(json_load(raw2)["order_id"])
    return None


def simulate_courier_position_report(
    ctx: LoadContext,
    courier_id: int,
    rng: random.Random,
) -> None:
    lat = rng.uniform(-23.80, -23.50)
    lon = rng.uniform(-46.95, -46.35)
    ts = int(time.time() * 1000)
    body = {
        "courier_id": courier_id,
        "timestamp": ts,
        "position": json.dumps({"lat": lat, "lon": lon}),
        "lat": lat,
        "lon": lon,
    }
    ctx.req("POST", "/courier-positions", body)


def simulate_order_status_progress(
    ctx: LoadContext,
    order_id: int,
    order_status_by_id: dict[int, int],
    status_lock: threading.Lock,
) -> bool:
    """
    Try POST /sim/orders/{id}/transition; on 404/501 use PUT + order-logs + GET verify.
    """
    with status_lock:
        current = order_status_by_id.get(order_id, 1)
    if current >= 6:
        return False
    next_id = current + 1

    code, _ = ctx.req(
        "POST",
        f"/sim/orders/{order_id}/transition",
        {"order_status_id": next_id, "detail": "load_test"},
    )
    if code in (200, 201):
        with status_lock:
            order_status_by_id[order_id] = next_id
        return True
    if code not in (404, 501):
        return False

    code_g, raw_g = ctx.req("GET", f"/orders/{order_id}")
    if code_g != 200:
        return False
    data = json_load(raw_g)
    if not isinstance(data, dict):
        return False

    body = {
        "customer_id": data["customer_id"],
        "food_place_id": data["food_place_id"],
        "courier_id": data["courier_id"],
        "order_status_id": next_id,
    }
    code_p, _ = ctx.req("PUT", f"/orders/{order_id}", body)
    if code_p != 200:
        return False

    ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    code_l, _ = ctx.req(
        "POST",
        "/order-logs",
        {
            "order_id": order_id,
            "timestamp": ts_iso,
            "order_status_id": next_id,
            "detail": "load_test progression",
        },
    )
    if code_l not in (200, 201):
        return False

    code_v, raw_v = ctx.req("GET", f"/orders/{order_id}")
    if code_v != 200:
        return False
    verified = json_load(raw_v)["order_status_id"] == next_id
    if verified:
        with status_lock:
            order_status_by_id[order_id] = next_id
    return verified
