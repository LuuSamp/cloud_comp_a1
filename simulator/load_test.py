"""
API load test: uses data from simulator.data_loader state file.

Places orders (optional dedicated rate), reports every non-offline courier position on a fixed
interval, and advances all open order statuses on a parallel sweep loop.

  pip install -r simulator/requirements.txt
  python -m simulator.data_loader --base-url http://<alb>
  python -m simulator.load_test --base-url http://<alb> [--state-file dijkfood_sim_state.json]

Example:
  python -m simulator.load_test --base-url http://alb/ --rate 3 --duration 30 --workers 2
  python -m simulator.load_test --base-url http://alb/ --orders-per-second 5 --rate 4 --duration 60
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .env_load import load_simulator_env
from .http_client import json_load
from .scenarios import (
    LoadContext,
    simulate_courier_position_report,
    simulate_order_status_progress,
    simulate_user_order,
)


def _courier_ids_for_position_report(ctx: LoadContext, seed_ids: list[int]) -> list[int]:
    """Couriers from state that are not offline (GET /couriers). Fallback: seed_ids if API fails."""
    want = set(seed_ids)
    active: list[int] = []
    skip = 0
    page = 200
    while True:
        code, raw = ctx.req("GET", f"/couriers?skip={skip}&limit={page}", timeout=60.0)
        if code != 200:
            return list(seed_ids)
        batch = json_load(raw)
        if not isinstance(batch, list):
            return list(seed_ids)
        for c in batch:
            if not isinstance(c, dict):
                continue
            try:
                cid = int(c["courier_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if cid not in want:
                continue
            st = str(c.get("status") or "").strip().lower()
            if st != "offline":
                active.append(cid)
        if len(batch) < page:
            break
        skip += page
    return active if active else list(seed_ids)


def _position_reporter(
    ctx: LoadContext,
    interval_s: float,
    stop: threading.Event,
    courier_ids: list[int],
) -> None:
    """Every `interval_s`, each active courier gets one position POST (parallel)."""
    n_c = len(courier_ids)
    max_workers = min(64, max(8, n_c)) if n_c else 1
    while True:
        if courier_ids:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                ex.map(
                    lambda cid: simulate_courier_position_report(
                        ctx, cid, random.Random(cid * 1_000_003 + 17)
                    ),
                    courier_ids,
                )
        if stop.wait(timeout=interval_s):
            break


def _order_placer(
    ctx: LoadContext,
    state: dict,
    deadline: float,
    orders_per_second: float,
    order_status_by_id: dict[int, int],
    order_lock: threading.Lock,
) -> None:
    """Steady aggregate rate of POST /place-order (drift-corrected spacing)."""
    rng = random.Random(424242)
    customers = state["customer_ids"]
    foods = state["food_place_ids"]
    n = len(customers)
    if n == 0:
        return
    interval = 1.0 / max(orders_per_second, 1e-9)
    next_wake = time.monotonic()
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        next_wake += interval
        sleep_for = max(0.0, min(next_wake, deadline) - now)
        if sleep_for > 0:
            time.sleep(sleep_for)
        if time.monotonic() >= deadline:
            break
        i = rng.randrange(n)
        oid = simulate_user_order(ctx, customers[i], foods[i])
        if oid is not None:
            with order_lock:
                order_status_by_id.setdefault(oid, 1)


def _status_advancer(
    ctx: LoadContext,
    deadline: float,
    order_status_by_id: dict[int, int],
    order_lock: threading.Lock,
    sweep_interval_s: float,
    max_parallel: int,
) -> None:
    """Repeatedly try one workflow step for every open order (parallel across orders)."""
    mw = max(1, max_parallel)
    while time.monotonic() < deadline:
        with order_lock:
            open_ids = [oid for oid, st in order_status_by_id.items() if st < 6]
        if open_ids:
            with ThreadPoolExecutor(max_workers=min(mw, len(open_ids))) as ex:
                futs = [
                    ex.submit(
                        simulate_order_status_progress,
                        ctx,
                        oid,
                        order_status_by_id,
                        order_lock,
                    )
                    for oid in open_ids
                ]
                for f in as_completed(futs):
                    try:
                        f.result()
                    except Exception:
                        pass
        time.sleep(sweep_interval_s)


def _scenario_worker(
    wid: int,
    ctx: LoadContext,
    state: dict,
    deadline: float,
    interval: float,
    order_status_by_id: dict[int, int],
    order_lock: threading.Lock,
    *,
    dedicated_order_placer: bool,
) -> None:
    if dedicated_order_placer:
        return
    rng = random.Random(wid * 10_007 + int(time.time()) % 9973)
    customers = state["customer_ids"]
    foods = state["food_place_ids"]
    n = len(customers)
    if n == 0:
        return

    while time.monotonic() < deadline:
        if rng.randint(0, 2) == 0:
            i = rng.randrange(n)
            oid = simulate_user_order(ctx, customers[i], foods[i])
            if oid is not None:
                with order_lock:
                    order_status_by_id.setdefault(oid, 1)
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
    tracking = (args.tracking_base_url or "").strip() or f"{base.rstrip('/')}/tracking"
    interval = 1.0 / max(args.rate, 0.001)
    dedicated_orders = args.orders_per_second is not None
    deadline = time.monotonic() + args.duration
    latencies: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    order_status_by_id: dict[int, int] = {}
    order_lock = threading.Lock()

    ctx = LoadContext(
        base,
        routing,
        tracking,
        latencies,
        errors,
        lock,
        args.debug_http,
    )

    reporting_couriers = _courier_ids_for_position_report(ctx, state["courier_ids"])

    stop_reporter = threading.Event()
    interval_s = max(args.position_interval_ms, 1.0) / 1000.0
    reporter = threading.Thread(
        target=_position_reporter,
        args=(
            ctx,
            interval_s,
            stop_reporter,
            reporting_couriers,
        ),
        daemon=True,
    )
    reporter.start()

    status_interval_s = max(args.status_interval_ms, 1.0) / 1000.0
    status_thread = threading.Thread(
        target=_status_advancer,
        args=(
            ctx,
            deadline,
            order_status_by_id,
            order_lock,
            status_interval_s,
            max(1, args.status_parallel),
        ),
        daemon=True,
    )
    status_thread.start()

    order_threads: list[threading.Thread] = []
    if dedicated_orders and args.orders_per_second > 0:
        ot = threading.Thread(
            target=_order_placer,
            args=(
                ctx,
                state,
                deadline,
                float(args.orders_per_second),
                order_status_by_id,
                order_lock,
            ),
            daemon=True,
        )
        ot.start()
        order_threads.append(ot)

    threads: list[threading.Thread] = []
    n_workers = 0 if dedicated_orders else args.workers
    for w in range(n_workers):
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
            kwargs={"dedicated_order_placer": dedicated_orders},
            daemon=True,
        )
        t.start()
        threads.append(t)

    for t in order_threads:
        t.join()
    for t in threads:
        t.join()
    stop_reporter.set()
    reporter.join(timeout=5.0)
    status_thread.join(timeout=5.0)

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
    p.add_argument(
        "--tracking-base-url",
        default=(os.environ.get("TRACKING_BASE_URL") or "").strip(),
        help="Tracking API base URL (default: <base-url>/tracking)",
    )
    p.add_argument(
        "--rate",
        type=float,
        default=2.0,
        help="Legacy place-order workers (only when --orders-per-second is omitted): "
        "iterations/s per worker; ~⅓ place an order per iteration",
    )
    p.add_argument(
        "--orders-per-second",
        type=float,
        default=None,
        metavar="N",
        help=(
            "Aggregate POST /place-order rate for the whole test. "
            "If set, one thread paces new orders at N/s; workers only run position/status. "
            "0 = no new orders (progress/positions only). "
            "Default (omit): workers only place orders (~rate×workers/3 orders/s); positions and "
            "status use dedicated loops."
        ),
    )
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
        help="Every active (non-offline) courier gets a position POST each interval (default 100 ms)",
    )
    p.add_argument(
        "--status-interval-ms",
        type=float,
        default=50.0,
        help="How often to sweep all open orders with a status-advance attempt (default 50 ms)",
    )
    p.add_argument(
        "--status-parallel",
        type=int,
        default=32,
        metavar="N",
        help="Max concurrent order status advance calls per sweep (default 32)",
    )
    p.add_argument(
        "--debug-http",
        action="store_true",
        help="On HTTP errors, print response body (truncated) to stderr",
    )
    args = p.parse_args()
    if not args.base_url:
        p.error("Pass --base-url or set BASE_URL in .env / connection.env")
    if args.orders_per_second is not None and args.orders_per_second < 0:
        p.error("--orders-per-second must be >= 0")
    sys.exit(run(args))


if __name__ == "__main__":
    main()
