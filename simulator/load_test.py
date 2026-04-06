"""
API load test: mixed CRUD on all entities plus high-frequency courier position posts.

Example:
  python -m simulator.load_test --base-url http://alb-xxx.elb.amazonaws.com \\
      --rate 5 --duration 30 --workers 2 --position-interval-ms 100
  python -m simulator.load_test ... --debug-http   # stderr: also dump HTTP error bodies
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

from dotenv import load_dotenv
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

# Set True from run() when --debug-http (read in _request from worker threads).
_debug_http: bool = False


def _url(base: str, path: str) -> str:
    return base.rstrip("/") + path


def _request(
    base: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    latencies: list[float],
    errors: list[str],
    lock: threading.Lock,
) -> tuple[int, bytes]:
    url = _url(base, path)
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            code = resp.status
            raw = resp.read()
        dt = time.perf_counter() - t0
        with lock:
            latencies.append(dt)
        return code, raw
    except urllib.error.HTTPError as e:
        dt = time.perf_counter() - t0
        try:
            raw_err = e.read()
        except Exception:
            raw_err = b""
        with lock:
            latencies.append(dt)
            errors.append(f"{method} {url} HTTP {e.code}")
        print(
            f"[load_test] debug HTTP {e.code} {method} {url}",
            file=sys.stderr,
            flush=True,
        )
        if _debug_http:
            snippet = raw_err.decode("utf-8", errors="replace")[:4000]
            if snippet.strip():
                print(snippet, file=sys.stderr, flush=True)
        return e.code, raw_err
    except Exception as e:
        dt = time.perf_counter() - t0
        with lock:
            latencies.append(dt)
            errors.append(f"{method} {url} {type(e).__name__}: {e}")
        print(
            f"[load_test] debug {type(e).__name__} {method} {url}: {e}",
            file=sys.stderr,
            flush=True,
        )
        return -1, b""


def _json_load(raw: bytes) -> Any:
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _post_position(
    base: str,
    courier_id: int,
    latencies: list[float],
    errors: list[str],
    lock: threading.Lock,
) -> None:
    ts = int(time.time() * 1000)
    body = {
        "courier_id": courier_id,
        "timestamp": ts,
        "position": json.dumps(
            {"lat": -23.5 + random.random() * 0.1, "lng": -46.6 + random.random() * 0.1}
        ),
    }
    _request(base, "POST", "/courier-positions", body, latencies, errors, lock)


def _scenario_worker(
    base: str,
    wid: int,
    deadline: float,
    interval: float,
    latencies: list[float],
    errors: list[str],
    lock: threading.Lock,
    courier_ids_lock: threading.Lock,
    active_courier_ids: list[int],
) -> None:
    tag = f"w{wid}-{uuid.uuid4().hex[:8]}"
    while time.monotonic() < deadline:
        cid = fpid = couid = oid = None
        log_id = str(uuid.uuid4())
        try:
            code, raw = _request(
                base,
                "POST",
                "/customers",
                {
                    "name": f"LT {tag}",
                    "email": f"{tag}@loadtest.invalid",
                    "phone": "555-0100",
                    "address": "Rua Teste 1",
                },
                latencies,
                errors,
                lock,
            )
            if code == 201:
                cid = _json_load(raw)["customer_id"]
            else:
                time.sleep(interval)
                continue

            code, raw = _request(
                base,
                "POST",
                "/food-places",
                {
                    "name": f"Place {tag}",
                    "kitchen_type": "UNSPECIFIED",
                    "address": "Av Teste 2",
                },
                latencies,
                errors,
                lock,
            )
            if code == 201:
                fpid = _json_load(raw)["food_place_id"]
            else:
                _request(base, "DELETE", f"/customers/{cid}", None, latencies, errors, lock)
                time.sleep(interval)
                continue

            code, raw = _request(
                base,
                "POST",
                "/couriers",
                {
                    "name": f"Courier {tag}",
                    "vehicle_type": "UNSPECIFIED",
                    "initial_address": "Rua 3",
                    "status": "idle",
                },
                latencies,
                errors,
                lock,
            )
            if code == 201:
                couid = _json_load(raw)["courier_id"]
                with courier_ids_lock:
                    if couid not in active_courier_ids:
                        active_courier_ids.append(couid)
            else:
                _request(base, "DELETE", f"/food-places/{fpid}", None, latencies, errors, lock)
                _request(base, "DELETE", f"/customers/{cid}", None, latencies, errors, lock)
                time.sleep(interval)
                continue

            code, raw = _request(
                base,
                "POST",
                "/orders",
                {
                    "customer_id": cid,
                    "food_place_id": fpid,
                    "courier_id": couid,
                    "status": "CONFIRMED",
                },
                latencies,
                errors,
                lock,
            )
            if code == 201:
                oid = _json_load(raw)["order_id"]
            else:
                couid_cleanup = couid
                _request(
                    base, "DELETE", f"/couriers/{couid_cleanup}", None, latencies, errors, lock
                )
                with courier_ids_lock:
                    if couid_cleanup in active_courier_ids:
                        active_courier_ids.remove(couid_cleanup)
                _request(base, "DELETE", f"/food-places/{fpid}", None, latencies, errors, lock)
                _request(base, "DELETE", f"/customers/{cid}", None, latencies, errors, lock)
                time.sleep(interval)
                continue

            _request(base, "GET", "/customers", None, latencies, errors, lock)
            _request(base, "GET", f"/customers/{cid}", None, latencies, errors, lock)
            _request(
                base,
                "PUT",
                f"/customers/{cid}",
                {
                    "name": f"LT {tag} U",
                    "email": f"{tag}@loadtest.invalid",
                    "phone": "555-0200",
                    "address": "Rua Updated",
                },
                latencies,
                errors,
                lock,
            )

            ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _request(
                base,
                "POST",
                "/order-logs",
                {
                    "order_log_id": log_id,
                    "order_id": oid,
                    "timestamp": ts_iso,
                    "status": "CONFIRMED",
                    "detail": "load_test",
                },
                latencies,
                errors,
                lock,
            )
            _request(
                base, "GET", f"/order-logs?order_id={oid}", None, latencies, errors, lock
            )
            _request(base, "GET", f"/order-logs/{log_id}", None, latencies, errors, lock)

            _post_position(base, couid, latencies, errors, lock)

            _request(base, "DELETE", f"/orders/{oid}", None, latencies, errors, lock)
            _request(base, "DELETE", f"/order-logs/{log_id}", None, latencies, errors, lock)
            _request(base, "DELETE", f"/couriers/{couid}", None, latencies, errors, lock)
            with courier_ids_lock:
                if couid in active_courier_ids:
                    active_courier_ids.remove(couid)
            _request(base, "DELETE", f"/food-places/{fpid}", None, latencies, errors, lock)
            _request(base, "DELETE", f"/customers/{cid}", None, latencies, errors, lock)

        except Exception as e:
            with lock:
                errors.append(f"scenario {tag}: {e}")
            if oid is not None:
                _request(base, "DELETE", f"/orders/{oid}", None, latencies, errors, lock)
            _request(
                base, "DELETE", f"/order-logs/{log_id}", None, latencies, errors, lock
            )
            if couid is not None:
                _request(
                    base, "DELETE", f"/couriers/{couid}", None, latencies, errors, lock
                )
                with courier_ids_lock:
                    if couid in active_courier_ids:
                        active_courier_ids.remove(couid)
            if fpid is not None:
                _request(
                    base, "DELETE", f"/food-places/{fpid}", None, latencies, errors, lock
                )
            if cid is not None:
                _request(
                    base, "DELETE", f"/customers/{cid}", None, latencies, errors, lock
                )

        time.sleep(interval)


def _position_reporter(
    base: str,
    interval_s: float,
    stop: threading.Event,
    latencies: list[float],
    errors: list[str],
    lock: threading.Lock,
    courier_ids_lock: threading.Lock,
    active_courier_ids: list[int],
) -> None:
    """Simulates ~100ms (or configured) position telemetry for active couriers."""
    while not stop.wait(timeout=interval_s):
        with courier_ids_lock:
            ids = list(active_courier_ids)
        if not ids:
            continue
        cid = ids[int(time.time() * 1000) % len(ids)]
        _post_position(base, cid, latencies, errors, lock)


def run(args: argparse.Namespace) -> int:
    global _debug_http
    _debug_http = args.debug_http

    base = args.base_url
    interval = 1.0 / max(args.rate, 0.001)
    deadline = time.monotonic() + args.duration
    latencies: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    courier_ids_lock = threading.Lock()
    active_courier_ids: list[int] = []

    stop_reporter = threading.Event()
    interval_s = max(args.position_interval_ms, 1.0) / 1000.0
    reporter = threading.Thread(
        target=_position_reporter,
        args=(
            base,
            interval_s,
            stop_reporter,
            latencies,
            errors,
            lock,
            courier_ids_lock,
            active_courier_ids,
        ),
        daemon=True,
    )
    reporter.start()

    threads: list[threading.Thread] = []
    for w in range(args.workers):
        t = threading.Thread(
            target=_scenario_worker,
            args=(
                base,
                w,
                deadline,
                interval,
                latencies,
                errors,
                lock,
                courier_ids_lock,
                active_courier_ids,
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
        f"[load_test] requests={n} errors={err_n} duration={elapsed:.2f}s rps={rps:.2f}"
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
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    p = argparse.ArgumentParser(description="DijkFood API CRUD load test")
    p.add_argument(
        "--base-url",
        default=(os.environ.get("BASE_URL") or "").strip(),
        help="API base URL (or set BASE_URL in project .env)",
    )
    p.add_argument("--rate", type=float, default=2.0, help="Scenario cycles per second per worker")
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument(
        "--position-interval-ms",
        type=float,
        default=100.0,
        help="Background courier position POST interval in ms (default 100)",
    )
    p.add_argument(
        "--debug-http",
        action="store_true",
        help="On HTTP errors, also print response body (truncated) to stderr after the debug line",
    )
    args = p.parse_args()
    if not args.base_url:
        p.error("Pass --base-url or set BASE_URL in project .env")
    sys.exit(run(args))


if __name__ == "__main__":
    main()
