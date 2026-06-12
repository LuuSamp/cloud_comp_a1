"""Customer simulation service: places orders based on discovered entities."""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import time

from simulator.shared.discovery import list_customers, list_food_places
from simulator.shared.env_load import load_simulator_env
from simulator.shared.http_client import request_json
from simulator.shared.runtime import SimulationRuntime
from simulator.shared.simulation_common import SimulationMetrics, sleep_until


def _print_periodic_stats(
    runtime: SimulationRuntime,
    latencies: list[float],
    requests: int,
    errors: int,
    *,
    at_sec: float,
    window_req: int,
    window_err: int,
) -> None:
    line = (
        f"[customer_sim][t+{at_sec:.1f}s] requests={requests} errors={errors} "
        f"window_requests={window_req} window_errors={window_err}"
    )
    if latencies:
        lat_ms = sorted(x * 1000.0 for x in latencies)
        p50 = statistics.median(lat_ms)
        p95 = lat_ms[max(0, int(round(0.95 * (len(lat_ms) - 1))))]
        line += (
            f" latency_ms(min/p50/p95/max)="
            f"{lat_ms[0]:.1f}/{p50:.1f}/{p95:.1f}/{lat_ms[-1]:.1f}"
        )
    line += f" base={runtime.ordering_base_url}"
    print(line)


def run(args: argparse.Namespace) -> int:
    runtime = SimulationRuntime.from_env(
        ordering_base_url=args.base_url,
        tracking_base_url=args.tracking_base_url,
        debug_http=args.debug_http,
    )
    rng = random.Random(args.seed)
    metrics = SimulationMetrics()
    latencies: list[float] = []
    log_every = max(float(args.log_interval_s), 1.0)

    start = time.monotonic()
    deadline = start + args.duration if args.duration > 0 else None
    interval = 1.0 / max(args.orders_per_second, 1e-9)
    next_tick = time.monotonic()
    next_log = start + log_every
    req_prev = 0
    err_prev = 0

    while True:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            break
        next_tick += interval

        customers = list_customers(runtime.ordering_base_url)
        foods = list_food_places(runtime.ordering_base_url)
        if not customers or not foods:
            metrics.add_errors()
            now = sleep_until(next_tick)
            if deadline is not None and now >= deadline:
                break
            continue

        c = rng.choice(customers)
        spike_id = (os.environ.get("SIM_SPIKE_FOOD_PLACE_ID") or "").strip()
        if spike_id.isdigit():
            spike_fp = [x for x in foods if int(x.get("food_place_id", -1)) == int(spike_id)]
            if spike_fp and rng.random() < float(os.environ.get("SIM_SPIKE_PROBABILITY", "0.7")):
                f = spike_fp[0]
            else:
                f = rng.choice(foods)
        else:
            f = rng.choice(foods)
        try:
            cid = int(c["customer_id"])
            fid = int(f["food_place_id"])
        except (KeyError, TypeError, ValueError):
            metrics.add_errors()
            now = sleep_until(next_tick)
            if deadline is not None and now >= deadline:
                break
            continue

        code, _ = request_json(
            runtime.ordering_base_url,
            "POST",
            "/place-order",
            body={"customer_id": cid, "food_place_id": fid, "order_status_id": 1},
            timeout=120.0,
            on_latency=lambda d: latencies.append(d),
            debug_http=runtime.debug_http,
        )
        metrics.add_requests()
        if code not in (200, 201):
            metrics.add_errors()

        now = sleep_until(next_tick)
        if now >= next_log:
            req, err = metrics.snapshot()
            _print_periodic_stats(
                runtime,
                latencies,
                req,
                err,
                at_sec=now - start,
                window_req=max(0, req - req_prev),
                window_err=max(0, err - err_prev),
            )
            req_prev = req
            err_prev = err
            latencies.clear()
            next_log += log_every
        if deadline is not None and now >= deadline:
            break

    req, err = metrics.snapshot()
    print(
        f"[customer_sim] base={runtime.ordering_base_url} requests={req} errors={err} "
        f"orders_per_second={args.orders_per_second}"
    )
    return 0 if req > 0 and err == 0 else (1 if req == 0 else 0)


def main() -> None:
    load_simulator_env()
    p = argparse.ArgumentParser(description="Customer simulation service (place-order)")
    p.add_argument("--base-url", default=(os.environ.get("BASE_URL") or "").strip())
    p.add_argument("--tracking-base-url", default=(os.environ.get("TRACKING_BASE_URL") or "").strip())
    p.add_argument("--orders-per-second", type=float, default=10.0)
    p.add_argument("--duration", type=float, default=0.0, help="Seconds (0=run forever)")
    p.add_argument(
        "--log-interval-s",
        type=float,
        default=10.0,
        help="Print periodic response-time stats every N seconds",
    )
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--debug-http", action="store_true")
    args = p.parse_args()
    if args.orders_per_second <= 0:
        p.error("--orders-per-second must be > 0")
    if not args.base_url:
        p.error("Pass --base-url or set BASE_URL")
    sys.exit(run(args))


if __name__ == "__main__":
    main()
