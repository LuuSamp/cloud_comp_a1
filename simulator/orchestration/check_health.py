"""
Probe health endpoints for ordering, routing, and tracking (same ALB by default).

  python -m simulator.orchestration.check_health --base-url http://<alb>
  python -m simulator.orchestration.check_health ... --strict-ready   # fail if routing graph not ready
"""

from __future__ import annotations

import argparse
import os
import sys

from simulator.shared.env_load import load_simulator_env
from simulator.shared.http_client import request_json


def main() -> None:
    load_simulator_env()
    p = argparse.ArgumentParser(description="Check DijkFood service health endpoints")
    p.add_argument(
        "--base-url",
        default=(os.environ.get("BASE_URL") or "").strip(),
        help="Ordering service / default host for all probes",
    )
    p.add_argument(
        "--routing-base-url",
        default="",
        help="Override host for routing paths (default: same as --base-url)",
    )
    p.add_argument(
        "--tracking-base-url",
        default="",
        help="Override host for tracking paths (default: same as --base-url)",
    )
    p.add_argument(
        "--strict-ready",
        action="store_true",
        help="Treat routing /routing/ready non-200 as failure",
    )
    args = p.parse_args()
    if not args.base_url:
        p.error("Pass --base-url or set BASE_URL in repo-root .env / connection.env")

    base = args.base_url.rstrip("/")
    routing = (args.routing_base_url or base).rstrip("/")
    tracking = (args.tracking_base_url or base).rstrip("/")

    liveness: list[tuple[str, str, str]] = [
        ("ordering", base, "/health"),
        ("routing", routing, "/routing/health"),
        ("tracking", tracking, "/tracking/health"),
    ]

    failed = False
    for name, host, path in liveness:
        code, _ = request_json(host, "GET", path, timeout=15.0)
        ok = code == 200
        status = "OK" if ok else f"FAIL ({code})"
        print(f"[check-health] {name} GET {path}: {status}")
        if not ok:
            failed = True

    ready_path = "/routing/ready"
    code_r, _ = request_json(routing, "GET", ready_path, timeout=15.0)
    if code_r == 200:
        print(f"[check-health] routing GET {ready_path}: OK (graph ready)")
    elif code_r == 503:
        msg = "WARN (graph loading or failed — see routing logs)"
        if args.strict_ready:
            msg = "FAIL (503 — graph not ready)"
            failed = True
        print(f"[check-health] routing GET {ready_path}: {msg}")
    else:
        print(f"[check-health] routing GET {ready_path}: FAIL ({code_r})")
        failed = True

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
