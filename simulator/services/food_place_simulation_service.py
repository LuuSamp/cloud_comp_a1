"""Food place simulation service: advances order status progression."""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from simulator.shared.discovery import list_orders
from simulator.shared.env_load import load_simulator_env
from simulator.shared.runtime import SimulationRuntime
from simulator.shared.scenarios import LoadContext, simulate_order_status_progress
from simulator.shared.simulation_common import sleep_until


def _print_periodic_stats(
    runtime: SimulationRuntime,
    latencies: list[float],
    requests: int,
    errors: int,
    *,
    tracked_orders: int,
    at_sec: float,
    window_req: int,
    window_err: int,
) -> None:
    line = (
        f"[food_place_sim][t+{at_sec:.1f}s] requests={requests} errors={errors} "
        f"window_requests={window_req} window_errors={window_err} "
        f"tracked_orders={tracked_orders}"
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


def run(args: argparse.Namespace) -> int:
    runtime = SimulationRuntime.from_env(
        ordering_base_url=args.base_url,
        tracking_base_url=args.tracking_base_url,
        debug_http=args.debug_http,
    )
    latencies: list[float] = []
    errors: list[str] = []
    metrics_lock = threading.Lock()
    status_lock = threading.Lock()
    ctx = LoadContext(
        runtime.ordering_base_url,
        None,
        runtime.tracking_base_url,
        latencies,
        errors,
        metrics_lock,
        runtime.debug_http,
    )

    statuses: dict[int, int] = {}
    start = time.monotonic()
    deadline = start + args.duration if args.duration > 0 else None

    next_discovery = time.monotonic()
    next_sweep = time.monotonic()
    next_log = start + max(float(args.log_interval_s), 1.0)
    discover_interval = max(args.discovery_interval_ms, 1.0) / 1000.0
    sweep_interval = max(args.status_interval_ms, 1.0) / 1000.0
    req_prev = 0
    err_prev = 0
    retry_counts: dict[int, int] = {}
    retry_due_at: dict[int, float] = {}
    rng = random.Random(42)

    while True:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            break

        if now >= next_discovery:
            next_discovery += discover_interval
            rows = list_orders(runtime.ordering_base_url)
            with status_lock:
                for row in rows:
                    try:
                        oid = int(row["order_id"])
                        sid = int(row["order_status_id"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    statuses[oid] = sid

        if now >= next_sweep:
            next_sweep += sweep_interval
            with status_lock:
                open_ids = [
                    oid
                    for oid, sid in statuses.items()
                    if sid < 6 and now >= retry_due_at.get(oid, 0.0)
                ]
            if open_ids:
                with ThreadPoolExecutor(max_workers=min(max(1, args.status_parallel), len(open_ids))) as ex:
                    fut_to_oid = {
                        ex.submit(
                            simulate_order_status_progress,
                            ctx,
                            oid,
                            statuses,
                            status_lock,
                        ): oid
                        for oid in open_ids
                    }
                    for f in as_completed(fut_to_oid):
                        oid = fut_to_oid[f]
                        try:
                            progressed, code = f.result()
                            if progressed:
                                retry_counts[oid] = 0
                                retry_due_at[oid] = 0.0
                                continue
                            if code in (409, 503, 504, -1):
                                n = retry_counts.get(oid, 0) + 1
                                retry_counts[oid] = n
                                backoff_s = min(
                                    max(0.2, float(args.retry_backoff_base_s)) * (2 ** (n - 1)),
                                    max(1.0, float(args.retry_backoff_max_s)),
                                )
                                jitter = backoff_s * rng.uniform(0.0, 0.3)
                                retry_due_at[oid] = time.monotonic() + backoff_s + jitter
                            else:
                                retry_counts[oid] = 0
                                retry_due_at[oid] = 0.0
                        except Exception:
                            with metrics_lock:
                                errors.append("status progression task crashed")

        now = sleep_until(min(next_discovery, next_sweep))
        if now >= next_log:
            with metrics_lock:
                req = len(latencies)
                err = len(errors)
            _print_periodic_stats(
                runtime,
                latencies[max(0, req_prev):req],
                req,
                err,
                tracked_orders=len(statuses),
                at_sec=now - start,
                window_req=max(0, req - req_prev),
                window_err=max(0, err - err_prev),
            )
            req_prev = req
            err_prev = err
            next_log += max(float(args.log_interval_s), 1.0)
        if deadline is not None and now >= deadline:
            break

    with metrics_lock:
        req = len(latencies)
        err = len(errors)
    print(
        f"[food_place_sim] ordering={runtime.ordering_base_url} tracking={runtime.tracking_base_url} "
        f"requests={req} errors={err} tracked_orders={len(statuses)}"
    )
    return 0 if req > 0 and err == 0 else (1 if req == 0 else 0)


def main() -> None:
    load_simulator_env()
    p = argparse.ArgumentParser(description="Food place simulation service (update-order-status)")
    p.add_argument("--base-url", default=(os.environ.get("BASE_URL") or "").strip())
    p.add_argument("--tracking-base-url", default=(os.environ.get("TRACKING_BASE_URL") or "").strip())
    p.add_argument("--duration", type=float, default=0.0, help="Seconds (0=run forever)")
    p.add_argument("--discovery-interval-ms", type=float, default=2000.0)
    p.add_argument("--status-interval-ms", type=float, default=500.0)
    p.add_argument("--status-parallel", type=int, default=12)
    p.add_argument("--retry-backoff-base-s", type=float, default=0.4)
    p.add_argument("--retry-backoff-max-s", type=float, default=6.0)
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
    if args.status_parallel < 1:
        p.error("--status-parallel must be >= 1")
    sys.exit(run(args))


if __name__ == "__main__":
    main()
