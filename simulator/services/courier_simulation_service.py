"""Courier simulation service: sends courier position reports."""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from simulator.shared.discovery import list_couriers, list_orders
from simulator.shared.env_load import load_simulator_env
from simulator.shared.http_client import json_load, request_json
from simulator.shared.runtime import SimulationRuntime
from simulator.shared.scenarios import LoadContext, simulate_courier_position_report
from simulator.shared.simulation_common import sleep_until


def _print_periodic_stats(
    runtime: SimulationRuntime,
    latencies: list[float],
    requests: int,
    errors: int,
    *,
    active_couriers: int,
    at_sec: float,
    window_req: int,
    window_err: int,
) -> None:
    line = (
        f"[courier_sim][t+{at_sec:.1f}s] requests={requests} errors={errors} "
        f"window_requests={window_req} window_errors={window_err} "
        f"active_couriers={active_couriers}"
    )
    if latencies:
        lat_ms = sorted(x * 1000.0 for x in latencies)
        p50 = statistics.median(lat_ms)
        p95 = lat_ms[max(0, int(round(0.95 * (len(lat_ms) - 1))))]
        line += (
            f" latency_ms(min/p50/p95/max)="
            f"{lat_ms[0]:.1f}/{p50:.1f}/{p95:.1f}/{lat_ms[-1]:.1f}"
        )
    print(line)


def _active_courier_ids(base_url: str) -> list[int]:
    out: list[int] = []
    for row in list_couriers(base_url):
        try:
            cid = int(row["courier_id"])
        except (KeyError, TypeError, ValueError):
            continue
        status = str(row.get("status") or "").strip().lower()
        if status != "offline":
            out.append(cid)
    return out


@dataclass
class CourierRouteState:
    order_id: int
    coordinates: list[tuple[float, float]]
    index: int = 0


def _routing_base_url(ordering_base_url: str) -> str:
    explicit = (os.environ.get("ROUTING_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    return f"{ordering_base_url.rstrip('/')}/routing"


def _assigned_orders_by_courier(base_url: str) -> dict[int, tuple[int, int, int]]:
    out: dict[int, tuple[int, int, int]] = {}
    for row in list_orders(base_url):
        try:
            cid_raw = row.get("courier_id")
            if cid_raw is None:
                continue
            cid = int(cid_raw)
            status_id = int(row["order_status_id"])
            if status_id >= 6:
                continue
            out[cid] = (
                int(row["order_id"]),
                int(row["customer_id"]),
                int(row["food_place_id"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _fetch_route_for_order(
    runtime: SimulationRuntime,
    *,
    order_id: int,
    customer_id: int,
    food_place_id: int,
) -> list[tuple[float, float]]:
    routing_base = _routing_base_url(runtime.ordering_base_url)
    query_order = urlencode({"order_id": order_id})
    code, raw = request_json(
        routing_base,
        "GET",
        f"/v1/get-route?{query_order}",
        timeout=20.0,
        debug_http=runtime.debug_http,
        quiet_http_statuses={503},
    )
    if code == 202:
        return []
    if code == 200:
        data = json_load(raw)
    else:
        query_pair = urlencode(
            {"customer_id": customer_id, "food_place_id": food_place_id}
        )
        code, raw = request_json(
            routing_base,
            "GET",
            f"/v1/get-route?{query_pair}",
            timeout=20.0,
            debug_http=runtime.debug_http,
            quiet_http_statuses={503},
        )
        if code == 202:
            return []
        if code != 200:
            return []
        data = json_load(raw)
    if not isinstance(data, dict):
        return []
    coords_raw = data.get("coordinates")
    if not isinstance(coords_raw, list):
        return []
    out: list[tuple[float, float]] = []
    for point in coords_raw:
        if not isinstance(point, dict):
            continue
        try:
            out.append((float(point["lat"]), float(point["lng"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def run(args: argparse.Namespace) -> int:
    runtime = SimulationRuntime.from_env(
        ordering_base_url=args.base_url,
        tracking_base_url=args.tracking_base_url,
        debug_http=args.debug_http,
    )
    latencies: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    ctx = LoadContext(
        runtime.ordering_base_url,
        None,
        runtime.tracking_base_url,
        latencies,
        errors,
        lock,
        runtime.debug_http,
    )

    start = time.monotonic()
    deadline = start + args.duration if args.duration > 0 else None
    next_discovery = time.monotonic()
    next_emit = time.monotonic()
    next_log = start + max(float(args.log_interval_s), 1.0)
    discover_interval = max(args.discovery_interval_ms, 1.0) / 1000.0
    emit_interval = max(args.position_interval_ms, 1.0) / 1000.0
    if (os.environ.get("SIM_COURIER_SHORTAGE") or "").strip().lower() in ("1", "true", "yes"):
        emit_interval *= float(os.environ.get("SIM_COURIER_SHORTAGE_FACTOR", "4"))
    courier_ids: list[int] = []
    assignments: dict[int, tuple[int, int, int]] = {}
    route_state: dict[int, CourierRouteState] = {}
    req_prev = 0
    err_prev = 0

    while True:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            break

        if now >= next_discovery:
            next_discovery += discover_interval
            courier_ids = _active_courier_ids(runtime.ordering_base_url)
            assignments = _assigned_orders_by_courier(runtime.ordering_base_url)
            active = set(courier_ids)
            route_state = {cid: st for cid, st in route_state.items() if cid in active}

        if now >= next_emit:
            next_emit += emit_interval
            if courier_ids:
                max_workers = min(max(1, args.position_parallel), len(courier_ids))
                targets: list[tuple[int, float, float]] = []
                for cid in courier_ids:
                    info = assignments.get(cid)
                    if info is None:
                        continue
                    order_id, customer_id, food_place_id = info
                    state = route_state.get(cid)
                    if state is None or state.order_id != order_id:
                        coords = _fetch_route_for_order(
                            runtime,
                            order_id=order_id,
                            customer_id=customer_id,
                            food_place_id=food_place_id,
                        )
                        if not coords:
                            continue
                        state = CourierRouteState(order_id=order_id, coordinates=coords)
                        route_state[cid] = state
                    if not state.coordinates:
                        continue
                    point = state.coordinates[min(state.index, len(state.coordinates) - 1)]
                    if state.index < len(state.coordinates) - 1:
                        state.index += 1
                    targets.append((cid, point[0], point[1]))
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    ex.map(
                        lambda item: simulate_courier_position_report(
                            ctx,
                            int(item[0]),
                            None,
                            lat=float(item[1]),
                            lon=float(item[2]),
                        ),
                        targets,
                    )

        now = sleep_until(min(next_discovery, next_emit))
        if now >= next_log:
            with lock:
                req = len(latencies)
                err = len(errors)
            _print_periodic_stats(
                runtime,
                latencies[max(0, req_prev):req],
                req,
                err,
                active_couriers=len(courier_ids),
                at_sec=now - start,
                window_req=max(0, req - req_prev),
                window_err=max(0, err - err_prev),
            )
            req_prev = req
            err_prev = err
            next_log += max(float(args.log_interval_s), 1.0)
        if deadline is not None and now >= deadline:
            break

    with lock:
        req = len(latencies)
        err = len(errors)
    print(
        f"[courier_sim] ordering={runtime.ordering_base_url} tracking={runtime.tracking_base_url} "
        f"requests={req} errors={err} active_couriers={len(courier_ids)}"
    )
    return 0 if req > 0 and err == 0 else (1 if req == 0 else 0)


def main() -> None:
    load_simulator_env()
    p = argparse.ArgumentParser(description="Courier simulation service (update-courier-position)")
    p.add_argument("--base-url", default=(os.environ.get("BASE_URL") or "").strip())
    p.add_argument("--tracking-base-url", default=(os.environ.get("TRACKING_BASE_URL") or "").strip())
    p.add_argument("--duration", type=float, default=0.0, help="Seconds (0=run forever)")
    p.add_argument("--discovery-interval-ms", type=float, default=2000.0)
    p.add_argument("--position-interval-ms", type=float, default=100.0)
    p.add_argument("--position-parallel", type=int, default=32)
    p.add_argument(
        "--log-interval-s",
        type=float,
        default=10.0,
        help="Print periodic response-time stats every N seconds",
    )
    p.add_argument("--debug-http", action="store_true")
    args = p.parse_args()
    if not args.base_url:
        p.error("Pass --base-url or set BASE_URL")
    if args.position_parallel < 1:
        p.error("--position-parallel must be >= 1")
    sys.exit(run(args))


if __name__ == "__main__":
    main()
