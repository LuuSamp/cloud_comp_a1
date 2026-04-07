"""
API load test: uses data from simulator.data_loader state file.

Runs three scenario types (place order, courier position, order progress + log + verify).

  pip install -r simulator/requirements.txt
  python -m simulator.data_loader --base-url http://<alb>
  python -m simulator.load_test --base-url http://<alb> [--state-file dijkfood_sim_state.json]

Example:
  python -m simulator.load_test --base-url http://alb/ --rate 3 --duration 30 --workers 2
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from pathlib import Path

from .env_load import load_simulator_env
from .scenarios import (
    LoadContext,
    simulate_courier_position_report,
    simulate_order_status_progress,
    simulate_user_order,
)


def _position_reporter(
    ctx: LoadContext,
    interval_s: float,
    stop: threading.Event,
    courier_ids: list[int],
    rng: random.Random,
) -> None:
    while not stop.wait(timeout=interval_s):
        if not courier_ids:
            continue
        cid = rng.choice(courier_ids)
        simulate_courier_position_report(ctx, cid, rng)


def _scenario_worker(
    wid: int,
    ctx: LoadContext,
    state: dict,
    deadline: float,
    interval: float,
    order_status_by_id: dict[int, int],
    order_lock: threading.Lock,
) -> None:
    rng = random.Random(wid * 10_007 + int(time.time()) % 9973)
    customers = state["customer_ids"]
    foods = state["food_place_ids"]
    couriers = state["courier_ids"]
    n = len(customers)
    if n == 0 or not couriers:
        return

    while time.monotonic() < deadline:
        kind = rng.randint(0, 2)
        if kind == 0:
            i = rng.randrange(n)
            oid = simulate_user_order(
                ctx,
                customers[i],
                foods[i],
                rng.choice(couriers),
            )
            if oid is not None:
                with order_lock:
                    order_status_by_id.setdefault(oid, 1)
        elif kind == 1:
            simulate_courier_position_report(ctx, rng.choice(couriers), rng)
        else:
            with order_lock:
                candidates = [o for o, s in order_status_by_id.items() if s < 6]
            if candidates:
                oid = rng.choice(candidates)
                simulate_order_status_progress(ctx, oid, order_status_by_id, order_lock)
        time.sleep(interval)


def run(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file)
    if not state_path.is_file():
        print(
            f"[load_test] Missing state file {state_path.resolve()}. "
            "Run: python -m simulator.data_loader --base-url <same ALB>",
            file=sys.stderr,
        )
        return 1

    state = json.loads(state_path.read_text(encoding="utf-8"))
    for key in ("customer_ids", "food_place_ids", "courier_ids"):
        if key not in state or not isinstance(state[key], list):
            print(f"[load_test] Invalid state file: missing {key} list", file=sys.stderr)
            return 1

    base = args.base_url
    routing = (args.routing_base_url or os.environ.get("ROUTING_BASE_URL") or "").strip() or None
    interval = 1.0 / max(args.rate, 0.001)
    deadline = time.monotonic() + args.duration
    latencies: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    order_status_by_id: dict[int, int] = {}
    order_lock = threading.Lock()

    ctx = LoadContext(
        base,
        routing,
        latencies,
        errors,
        lock,
        args.debug_http,
    )

    stop_reporter = threading.Event()
    interval_s = max(args.position_interval_ms, 1.0) / 1000.0
    rep_rng = random.Random(42)
    reporter = threading.Thread(
        target=_position_reporter,
        args=(
            ctx,
            interval_s,
            stop_reporter,
            state["courier_ids"],
            rep_rng,
        ),
        daemon=True,
    )
    reporter.start()

    threads: list[threading.Thread] = []
    for w in range(args.workers):
        t = threading.Thread(
            target=_scenario_worker,
            args=(
                w,
                ctx,
                state,
                deadline,
                interval,
                order_status_by_id,
                order_lock,
            ),
            daemon=True,
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join()
    stop_reporter.set()
    reporter.join(timeout=5.0)

    with lock:
        n = len(latencies)
        err_n = len(errors)
    elapsed = args.duration
    rps = n / elapsed if elapsed > 0 else 0.0
    print(
        f"[load_test] requests={n} errors={err_n} duration={elapsed:.2f}s rps={rps:.2f} "
        f"orders_touched={len(order_status_by_id)}"
    )
    if latencies:
        lat_ms = sorted(x * 1000 for x in latencies)
        p50 = lat_ms[len(lat_ms) // 2]
        p95_idx = max(0, int(round(0.95 * (len(lat_ms) - 1))))
        p95 = lat_ms[p95_idx]
        print(
            f"[load_test] latency ms: min={lat_ms[0]:.1f} p50={p50:.1f} "
            f"p95={p95:.1f} max={lat_ms[-1]:.1f}"
        )
    for e in errors[:15]:
        print(f"[load_test] error: {e}")
    if err_n > 0:
        return 1
    if n == 0:
        return 1
    return 0


def main() -> None:
    load_simulator_env()
    p = argparse.ArgumentParser(description="DijkFood scenario load test (requires data_loader state)")
    p.add_argument(
        "--base-url",
        default=(os.environ.get("BASE_URL") or "").strip(),
        help="Ordering API base URL",
    )
    p.add_argument(
        "--routing-base-url",
        default=(os.environ.get("ROUTING_BASE_URL") or "").strip(),
        help="If set, POST /orders may omit courier_id (nearest-courier assignment)",
    )
    p.add_argument("--rate", type=float, default=2.0, help="Scenario steps per second per worker")
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument(
        "--state-file",
        default="dijkfood_sim_state.json",
        help="JSON from simulator.data_loader",
    )
    p.add_argument(
        "--position-interval-ms",
        type=float,
        default=100.0,
        help="Background courier position POST interval in ms (default 100)",
    )
    p.add_argument(
        "--debug-http",
        action="store_true",
        help="On HTTP errors, print response body (truncated) to stderr",
    )
    args = p.parse_args()
    if not args.base_url:
        p.error("Pass --base-url or set BASE_URL in .env / connection.env")
    sys.exit(run(args))


if __name__ == "__main__":
    main()
