"""
Configurable HTTP load against the deployed API: measures latency and throughput.

Example:
  python -m simulator.load_sim --base-url http://alb-xxx.us-east-1.elb.amazonaws.com \\
      --rate 5 --duration 10
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys

import threading
import time
import urllib.error
import urllib.request

from .env_load import load_simulator_env


def _fetch(url: str, path: str, latencies: list[float], errors: list[str]) -> None:
    full = url.rstrip("/") + path
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(full, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read(64)
        latencies.append(time.perf_counter() - t0)
    except urllib.error.HTTPError as e:
        errors.append(f"HTTP {e.code} {full}")
    except Exception as e:
        errors.append(f"{type(e).__name__}: {e}")


def run(args: argparse.Namespace) -> int:
    base = args.base_url
    interval = 1.0 / max(args.rate, 0.001)
    deadline = time.monotonic() + args.duration
    latencies: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    threads: list[threading.Thread] = []

    def worker() -> None:
        while time.monotonic() < deadline:
            local_lat: list[float] = []
            local_err: list[str] = []
            _fetch(base, args.path, local_lat, local_err)
            with lock:
                latencies.extend(local_lat)
                errors.extend(local_err)
            time.sleep(interval)

    print(
        f"[sim] base={base} path={args.path} rate={args.rate}/s "
        f"duration={args.duration}s workers={args.workers}"
    )
    t_start = time.monotonic()
    for _ in range(args.workers):
        th = threading.Thread(target=worker, daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    elapsed = time.monotonic() - t_start

    with lock:
        n = len(latencies)
        err_n = len(errors)
    rps = n / elapsed if elapsed > 0 else 0.0
    print(f"[sim] completed requests={n} errors={err_n} elapsed={elapsed:.2f}s rps={rps:.2f}")
    if latencies:
        lat_ms = sorted(x * 1000 for x in latencies)
        p50 = statistics.median(lat_ms)
        p95_idx = max(0, int(round(0.95 * (len(lat_ms) - 1))))
        p95 = lat_ms[p95_idx]
        print(
            f"[sim] latency ms: min={lat_ms[0]:.1f} "
            f"p50={p50:.1f} p95={p95:.1f} max={lat_ms[-1]:.1f}"
        )
    for e in errors[:10]:
        print(f"[sim] error: {e}")
    if err_n > 0:
        return 1
    if n == 0:
        return 1
    return 0


def main() -> None:
    load_simulator_env()
    p = argparse.ArgumentParser(description="DijkFood HTTP load simulator")
    p.add_argument(
        "--base-url",
        default=(os.environ.get("BASE_URL") or "").strip(),
        help="e.g. http://my-alb.us-east-1.elb.amazonaws.com (or BASE_URL in .env / connection.env)",
    )
    p.add_argument("--path", default="/health", help="URL path to request")
    p.add_argument("--rate", type=float, default=10.0, help="Target requests per second per worker")
    p.add_argument("--duration", type=float, default=15.0, help="Run time in seconds")
    p.add_argument("--workers", type=int, default=2, help="Concurrent worker threads")
    args = p.parse_args()
    if not args.base_url:
        p.error("Pass --base-url or set BASE_URL in .env / connection.env")
    sys.exit(run(args))


if __name__ == "__main__":
    main()
