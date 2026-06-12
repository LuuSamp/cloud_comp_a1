"""
Simulation orchestrator: runs customer/food_place/courier service loops together.

This replaces local-state orchestration; each service discovers entities via API GET endpoints.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from simulator.shared.env_load import load_simulator_env
from simulator.services.courier_simulation_service import run as run_courier
from simulator.services.customer_simulation_service import run as run_customer
from simulator.services.food_place_simulation_service import run as run_food_place


def run(args: argparse.Namespace) -> int:
    results: dict[str, int] = {}
    lock = threading.Lock()

    def _runner(name: str, ns: argparse.Namespace, fn) -> None:
        code = fn(ns)
        with lock:
            results[name] = code

    base = args.base_url
    tracking = (args.tracking_base_url or "").strip() or f"{base.rstrip('/')}/tracking"
    common = {
        "base_url": base,
        "tracking_base_url": tracking,
        "duration": args.duration,
        "log_interval_s": args.log_interval_s,
        "debug_http": args.debug_http,
    }
    customer_ns = argparse.Namespace(
        **common,
        orders_per_second=args.orders_per_second,
        seed=args.seed,
    )
    food_ns = argparse.Namespace(
        **common,
        discovery_interval_ms=args.discovery_interval_ms,
        status_interval_ms=args.status_interval_ms,
        status_parallel=args.status_parallel,
        retry_backoff_base_s=args.retry_backoff_base_s,
        retry_backoff_max_s=args.retry_backoff_max_s,
    )
    courier_ns = argparse.Namespace(
        **common,
        discovery_interval_ms=args.discovery_interval_ms,
        position_interval_ms=args.position_interval_ms,
        position_parallel=args.position_parallel,
    )

    threads = [
        threading.Thread(target=_runner, args=("customer", customer_ns, run_customer), daemon=True),
        threading.Thread(target=_runner, args=("food_place", food_ns, run_food_place), daemon=True),
        threading.Thread(target=_runner, args=("courier", courier_ns, run_courier), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(
        "[load_test] service exit codes: "
        + ", ".join(f"{k}={v}" for k, v in sorted(results.items()))
    )
    return 0 if all(v == 0 for v in results.values()) else 1


def main() -> None:
    load_simulator_env()
    p = argparse.ArgumentParser(description="DijkFood simulation orchestrator")
    p.add_argument(
        "--base-url",
        default=(os.environ.get("BASE_URL") or "").strip(),
        help="Ordering API base URL",
    )
    p.add_argument(
        "--tracking-base-url",
        default=(os.environ.get("TRACKING_BASE_URL") or "").strip(),
        help="Tracking API base URL (default: <base-url>/tracking)",
    )
    p.add_argument("--duration", type=float, default=0.0, help="Seconds (0=run forever)")
    p.add_argument("--orders-per-second", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--discovery-interval-ms", type=float, default=2000.0)
    p.add_argument(
        "--position-interval-ms",
        type=float,
        default=250.0,
    )
    p.add_argument("--position-parallel", type=int, default=16)
    p.add_argument("--status-interval-ms", type=float, default=500.0)
    p.add_argument("--status-parallel", type=int, default=8)
    p.add_argument("--retry-backoff-base-s", type=float, default=0.4)
    p.add_argument("--retry-backoff-max-s", type=float, default=6.0)
    p.add_argument(
        "--log-interval-s",
        type=float,
        default=10.0,
        help="Print periodic response-time stats every N seconds per simulator service",
    )
    p.add_argument(
        "--debug-http",
        action="store_true",
        help="On HTTP errors, print response body (truncated) to stderr",
    )
    p.add_argument(
        "--spike-region",
        type=int,
        default=0,
        metavar="FOOD_PLACE_ID",
        help="Concentrate orders on one restaurant (sets SIM_SPIKE_FOOD_PLACE_ID)",
    )
    p.add_argument(
        "--spike-probability",
        type=float,
        default=0.7,
        help="Probability of choosing --spike-region restaurant when set",
    )
    p.add_argument(
        "--courier-shortage",
        action="store_true",
        help="Slow courier position updates to simulate reduced availability",
    )
    p.add_argument(
        "--courier-shortage-factor",
        type=float,
        default=4.0,
        help="Multiplier on position interval when --courier-shortage is set",
    )
    args = p.parse_args()
    if args.spike_region > 0:
        os.environ["SIM_SPIKE_FOOD_PLACE_ID"] = str(args.spike_region)
        os.environ["SIM_SPIKE_PROBABILITY"] = str(args.spike_probability)
    if args.courier_shortage:
        os.environ["SIM_COURIER_SHORTAGE"] = "1"
        os.environ["SIM_COURIER_SHORTAGE_FACTOR"] = str(args.courier_shortage_factor)
    if not args.base_url:
        p.error("Pass --base-url or set BASE_URL in repo-root .env / connection.env")
    if args.orders_per_second <= 0:
        p.error("--orders-per-second must be > 0")
    sys.exit(run(args))


if __name__ == "__main__":
    main()
