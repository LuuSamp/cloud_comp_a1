"""
Load RDS fixture data via public POST APIs (no orders).

  pip install -r simulator/requirements.txt
  python -m simulator.data_loader --base-url http://<alb> [--customers 10]

Writes dijkfood_sim_state.json (or --state-file) for load_test.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import uuid
from pathlib import Path

from .env_load import load_simulator_env
from .http_client import json_load, request_json
from .sp_coords import sample_coordinates


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
        p.error("Pass --base-url or set BASE_URL in .env / connection.env")

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

    for i in range(n_c):
        lat, lon = next_coord()
        suffix = f"{run_id}-{i}"
        email = f"sim.{suffix}@dijkfood.invalid"
        body = {
            "name": fake.name(),
            "email": email,
            "phone": fake.phone_number()[:32] or "555-0000",
            "address": fake.address().replace("\n", ", ")[:500],
            "lat": lat,
            "lon": lon,
        }
        code, raw = request_json(args.base_url, "POST", "/customers", body=body, timeout=60.0)
        if code != 201:
            print(f"POST /customers failed HTTP {code}: {raw[:500]!r}", file=sys.stderr)
            sys.exit(1)
        customer_ids.append(int(json_load(raw)["customer_id"]))

    for i in range(n_c):
        lat, lon = next_coord()
        body = {
            "name": f"{fake.company()[:80]} {run_id}-{i}",
            "kitchen_type": "UNSPECIFIED",
            "address": fake.address().replace("\n", ", ")[:500],
            "lat": lat,
            "lon": lon,
        }
        code, raw = request_json(args.base_url, "POST", "/food-places", body=body, timeout=60.0)
        if code != 201:
            print(f"POST /food-places failed HTTP {code}: {raw[:500]!r}", file=sys.stderr)
            sys.exit(1)
        food_place_ids.append(int(json_load(raw)["food_place_id"]))

    for i in range(n_couriers):
        status = "offline" if i % 2 == 0 else "available"
        lat, lon = next_coord()
        body = {
            "name": f"Courier {fake.first_name()} {run_id}-{i}",
            "vehicle_type": "UNSPECIFIED",
            "initial_address": fake.street_address()[:255],
            "status": status,
            "initial_lat": lat,
            "initial_lon": lon,
        }
        code, raw = request_json(args.base_url, "POST", "/couriers", body=body, timeout=60.0)
        if code != 201:
            print(f"POST /couriers failed HTTP {code}: {raw[:500]!r}", file=sys.stderr)
            sys.exit(1)
        courier_ids.append(int(json_load(raw)["courier_id"]))

    state = {
        "customer_ids": customer_ids,
        "food_place_ids": food_place_ids,
        "courier_ids": courier_ids,
        "meta": {
            "customers": n_c,
            "courier_factor": args.courier_factor,
            "run_id": run_id,
        },
    }
    out_path = Path(args.state_file)
    out_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(
        f"[data_loader] wrote {out_path}: {n_c} customers, {n_c} food places, "
        f"{n_couriers} couriers (no orders)"
    )


if __name__ == "__main__":
    main()
