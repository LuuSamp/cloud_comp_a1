"""
Load-test scenario steps: place order, courier position, order lifecycle + log + verify.
"""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Any

from simulator.shared.http_client import json_load, request_json

# Ordering nearest-courier can wait on routing for up to ~120s; tracking also POSTs assign on →3.
ORDER_FLOW_HTTP_TIMEOUT_S = 120.0


class LoadContext:
    """HTTP + metrics for one load-test run."""

    def __init__(
        self,
        base_url: str,
        routing_base_url: str | None,
        tracking_base_url: str,
        latencies: list[float],
        errors: list[str],
        lock: threading.Lock,
        debug_http: bool,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.routing_base_url = (routing_base_url or "").strip() or None
        self.tracking_base_url = tracking_base_url.rstrip("/")
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

    def req_tracking(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 60.0,
        quiet_http_statuses: set[int] | None = None,
    ) -> tuple[int, bytes]:
        def on_lat(d: float) -> None:
            with self.lock:
                self.latencies.append(d)

        def on_err(m: str) -> None:
            with self.lock:
                self.errors.append(m)

        return request_json(
            self.tracking_base_url,
            method,
            path,
            body=body,
            timeout=timeout,
            on_latency=on_lat,
            on_error=on_err,
            debug_http=self.debug_http,
            quiet_http_statuses=quiet_http_statuses,
        )


def _order_status_from_api(ctx: LoadContext, order_id: int, *, timeout: float) -> int | None:
    code, raw = ctx.req("GET", f"/orders/{order_id}", timeout=timeout)
    if code == 200:
        data = json_load(raw)
        if isinstance(data, dict) and data.get("order_status_id") is not None:
            return int(data["order_status_id"])
    code, raw = ctx.req_tracking(
        "GET",
        f"/get-order-status?order_id={order_id}",
        timeout=timeout,
    )
    if code == 200:
        data = json_load(raw)
        if isinstance(data, dict) and data.get("order_status_id") is not None:
            return int(data["order_status_id"])
    return None


def _sync_local_order_status(
    ctx: LoadContext,
    order_id: int,
    order_status_by_id: dict[int, int],
    status_lock: threading.Lock,
    *,
    timeout: float,
) -> None:
    sid = _order_status_from_api(ctx, order_id, timeout=timeout)
    if sid is not None:
        with status_lock:
            order_status_by_id[order_id] = sid


def simulate_user_order(
    ctx: LoadContext,
    customer_id: int,
    food_place_id: int,
) -> int | None:
    code, raw = ctx.req(
        "POST",
        "/place-order",
        {
            "customer_id": customer_id,
            "food_place_id": food_place_id,
            "order_status_id": 1,
        },
    )
    if code in (200, 201):
        data = json_load(raw)
        if isinstance(data, dict) and "order_id" in data:
            return int(data["order_id"])
    return None


def simulate_courier_position_report(
    ctx: LoadContext,
    courier_id: int,
    rng: random.Random | None = None,
    *,
    lat: float | None = None,
    lon: float | None = None,
) -> None:
    if lat is None or lon is None:
        if rng is None:
            rng = random.Random()
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
    ctx.req_tracking("POST", "/update-courier-position", body)


def simulate_order_status_progress(
    ctx: LoadContext,
    order_id: int,
    order_status_by_id: dict[int, int],
    status_lock: threading.Lock,
) -> tuple[bool, int]:
    to = ORDER_FLOW_HTTP_TIMEOUT_S
    with status_lock:
        current = order_status_by_id.get(order_id, 1)
    if current >= 6:
        return False, 200
    next_id = current + 1

    code, _ = ctx.req_tracking(
        "POST",
        "/update-order-status",
        {"order_id": order_id, "order_status_id": next_id, "detail": "load_test"},
        timeout=to,
        quiet_http_statuses={409, 503},
    )
    if code != 200 and code != 201:
        _sync_local_order_status(ctx, order_id, order_status_by_id, status_lock, timeout=to)
        return False, code

    code_v, raw_v = ctx.req_tracking(
        "GET",
        f"/get-order-status?order_id={order_id}",
        timeout=to,
    )
    if code_v == 200:
        data = json_load(raw_v)
        status_id = data.get("order_status_id") if isinstance(data, dict) else None
        if status_id is not None:
            got = int(status_id)
            if got == next_id:
                with status_lock:
                    order_status_by_id[order_id] = next_id
                return True, code
            if got > next_id:
                with status_lock:
                    order_status_by_id[order_id] = got
                return True, code

    code_order, raw_order = ctx.req("GET", f"/orders/{order_id}", timeout=to)
    if code_order == 200:
        data = json_load(raw_order)
        got = data.get("order_status_id") if isinstance(data, dict) else None
        if got is not None:
            got = int(got)
            if got == next_id:
                with status_lock:
                    order_status_by_id[order_id] = next_id
                return True, code
            if got > next_id:
                with status_lock:
                    order_status_by_id[order_id] = got
                return True, code

    _sync_local_order_status(ctx, order_id, order_status_by_id, status_lock, timeout=to)
    return False, code
