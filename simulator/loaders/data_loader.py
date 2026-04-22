"""
Load RDS fixture data via public POST APIs (no orders).

  pip install -r simulator/requirements.txt
  python -m simulator.loaders.data_loader --base-url http://<alb> [--customers 10]

Writes dijkfood_sim_state.json (or --state-file) for load_test.

If the state file already exists, deletes tracked customers, food_places, couriers
(and any orders referencing them) via API before inserting, so re-runs do not
leave stale rows or duplicate fixture conflicts.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import uuid
from pathlib import Path
from typing import Any

from simulator.shared.env_load import load_simulator_env
from simulator.shared.http_client import json_load, request_json
from simulator.loaders.sp_coords import sample_coordinates


def _load_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[data_loader] WARN: could not parse {path}: {e}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _delete_tracked_entities(base_url: str, state: dict[str, Any]) -> None:
    """Remove prior fixture rows so this run can POST afresh without FK conflicts."""
    customers = {int(x) for x in (state.get("customer_ids") or []) if x is not None}
    foods = {int(x) for x in (state.get("food_place_ids") or []) if x is not None}
    couriers = {int(x) for x in (state.get("courier_ids") or []) if x is not None}
    if not customers and not foods and not couriers:
        return

    print(
        f"[data_loader] Deleting prior fixture: "
        f"{len(customers)} customers, {len(foods)} food_places, {len(couriers)} couriers"
    )

    order_ids: list[int] = []
    skip = 0
    page_limit = 200
    while True:
        code, raw = request_json(
            base_url,
            "GET",
            f"/orders?skip={skip}&limit={page_limit}",
            timeout=120.0,
        )
        if code != 200:
            print(
                f"[data_loader] GET /orders failed HTTP {code}: {raw[:300]!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        batch = json_load(raw)
        if not isinstance(batch, list) or not batch:
            break
        for o in batch:
            if not isinstance(o, dict):
                continue
            try:
                oid = int(o["order_id"])
                cid = int(o["customer_id"])
                fid = int(o["food_place_id"])
                coid = o.get("courier_id")
                coid_i = int(coid) if coid is not None else None
            except (KeyError, TypeError, ValueError):
                continue
            if (
                cid in customers
                or fid in foods
                or (coid_i is not None and coid_i in couriers)
            ):
                order_ids.append(oid)
        if len(batch) < page_limit:
            break
        skip += page_limit

    uniq_orders = sorted(set(order_ids))
    if uniq_orders:
        code, raw = request_json(
            base_url,
            "POST",
            "/orders/bulk-delete",
            body={"order_ids": uniq_orders},
            timeout=120.0,
        )
        if code != 200:
            print(
                f"[data_loader] POST /orders/bulk-delete failed HTTP {code}: {raw[:500]!r}",
                file=sys.stderr,
            )

    if couriers:
        code, raw = request_json(
            base_url,
            "POST",
            "/couriers/bulk-delete",
            body={"ids": sorted(couriers)},
            timeout=120.0,
        )
        if code != 200:
            print(
                f"[data_loader] POST /couriers/bulk-delete failed HTTP {code}: {raw[:500]!r}",
                file=sys.stderr,
            )

    if foods:
        code, raw = request_json(
            base_url,
            "POST",
            "/food-places/bulk-delete",
            body={"ids": sorted(foods)},
            timeout=120.0,
        )
        if code != 200:
            print(
                f"[data_loader] POST /food-places/bulk-delete failed HTTP {code}: {raw[:500]!r}",
                file=sys.stderr,
            )

    if customers:
        code, raw = request_json(
            base_url,
            "POST",
            "/customers/bulk-delete",
            body={"ids": sorted(customers)},
            timeout=120.0,
        )
        if code != 200:
            print(
                f"[data_loader] POST /customers/bulk-delete failed HTTP {code}: {raw[:500]!r}",
                file=sys.stderr,
            )


def main() -> None:
    load_simulator_env()
    p = argparse.ArgumentParser(description="Insert customers, food_places, couriers via API")
    p.add_argument(
        "--base-url",
        default=(os.environ.get("BASE_URL") or "").strip(),
        help="Ordering API base URL",
    )
    p.add_argument(
        "--routing-base-url",
        default=(os.environ.get("ROUTING_BASE_URL") or "").strip(),
        help="Same ALB as base-url by default; used for /routing/v1/random-points",
    )
    p.add_argument("--customers", type=int, default=10, help="Number of customers (and food places)")
    p.add_argument(
        "--courier-factor",
        type=int,
        default=3,
        help="Couriers = factor * customers (default 3)",
    )
    p.add_argument("--seed", type=int, default=None, help="Faker / random seed")
    p.add_argument(
        "--state-file",
        default="dijkfood_sim_state.json",
        help="Output JSON for load_test",
    )
    args = p.parse_args()
    if not args.base_url:
        p.error("Pass --base-url or set BASE_URL in repo-root .env / connection.env")

    try:
        from faker import Faker
    except ImportError:
        print("Install Faker: pip install -r simulator/requirements.txt", file=sys.stderr)
        sys.exit(1)

    rng = random.Random(args.seed)
    if args.seed is not None:
        Faker.seed(args.seed)
    fake = Faker("pt_BR")

    n_c = args.customers
    n_couriers = n_c * args.courier_factor
    if n_c < 1:
        p.error("--customers must be at least 1")

    out_path = Path(args.state_file)
    previous = _load_state(out_path)
    if previous:
        _delete_tracked_entities(args.base_url, previous)

    routing_url = args.routing_base_url or args.base_url
    pool_n = n_c + n_c + n_couriers
    coord_pool = sample_coordinates(pool_n, routing_url, rng=rng)
    ci = 0

    def next_coord() -> tuple[float, float]:
        nonlocal ci
        lat, lon = coord_pool[ci % len(coord_pool)]
        ci += 1
        return lat, lon

    customer_ids: list[int] = []
    food_place_ids: list[int] = []
    courier_ids: list[int] = []

    run_id = uuid.uuid4().hex[:8]

    customer_items: list[dict[str, Any]] = []
    for i in range(n_c):
        lat, lon = next_coord()
        suffix = f"{run_id}-{i}"
        email = f"sim.{suffix}@dijkfood.invalid"
        customer_items.append(
            {
                "name": fake.name(),
                "email": email,
                "phone": fake.phone_number()[:32] or "555-0000",
                "address": fake.address().replace("\n", ", ")[:500],
                "lat": lat,
                "lon": lon,
            }
        )
    code, raw = request_json(
        args.base_url,
        "POST",
        "/customers/bulk",
        body={"items": customer_items},
        timeout=120.0,
    )
    if code != 201:
        print(f"POST /customers/bulk failed HTTP {code}: {raw[:500]!r}", file=sys.stderr)
        sys.exit(1)
    cust_rows = json_load(raw)
    if not isinstance(cust_rows, list):
        print("[data_loader] unexpected /customers/bulk response", file=sys.stderr)
        sys.exit(1)
    customer_ids = [int(r["customer_id"]) for r in cust_rows]

    food_items: list[dict[str, Any]] = []
    for i in range(n_c):
        lat, lon = next_coord()
        food_items.append(
            {
                "name": f"{fake.company()[:80]} {run_id}-{i}",
                "kitchen_type": "UNSPECIFIED",
                "address": fake.address().replace("\n", ", ")[:500],
                "lat": lat,
                "lon": lon,
            }
        )
    code, raw = request_json(
        args.base_url,
        "POST",
        "/food-places/bulk",
        body={"items": food_items},
        timeout=120.0,
    )
    if code != 201:
        print(f"POST /food-places/bulk failed HTTP {code}: {raw[:500]!r}", file=sys.stderr)
        sys.exit(1)
    fp_rows = json_load(raw)
    if not isinstance(fp_rows, list):
        print("[data_loader] unexpected /food-places/bulk response", file=sys.stderr)
        sys.exit(1)
    food_place_ids = [int(r["food_place_id"]) for r in fp_rows]

    courier_items: list[dict[str, Any]] = []
    for i in range(n_couriers):
        status = "offline" if i % 2 == 0 else "available"
        lat, lon = next_coord()
        courier_items.append(
            {
                "name": f"Courier {fake.first_name()} {run_id}-{i}",
                "vehicle_type": "UNSPECIFIED",
                "initial_address": fake.street_address()[:255],
                "status": status,
                "initial_lat": lat,
                "initial_lon": lon,
            }
        )
    code, raw = request_json(
        args.base_url,
        "POST",
        "/couriers/bulk",
        body={"items": courier_items},
        timeout=120.0,
    )
    if code != 201:
        print(f"POST /couriers/bulk failed HTTP {code}: {raw[:500]!r}", file=sys.stderr)
        sys.exit(1)
    c_rows = json_load(raw)
    if not isinstance(c_rows, list):
        print("[data_loader] unexpected /couriers/bulk response", file=sys.stderr)
        sys.exit(1)
    courier_ids = [int(r["courier_id"]) for r in c_rows]

    prev_runs: list[Any] = []
    if previous and isinstance(previous.get("meta"), dict):
        pr = previous["meta"].get("runs")
        if isinstance(pr, list):
            prev_runs = pr

    state = {
        "customer_ids": customer_ids,
        "food_place_ids": food_place_ids,
        "courier_ids": courier_ids,
        "meta": {
            "customers": n_c,
            "courier_factor": args.courier_factor,
            "run_id": run_id,
            "runs": prev_runs
            + [
                {
                    "run_id": run_id,
                    "customers": n_c,
                    "courier_factor": args.courier_factor,
                    "replaced_previous": bool(previous),
                }
            ],
        },
    }
    out_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(
        f"[data_loader] wrote {out_path}: {n_c} customers, {n_c} food places, "
        f"{n_couriers} couriers (no orders)"
    )


if __name__ == "__main__":
    main()
