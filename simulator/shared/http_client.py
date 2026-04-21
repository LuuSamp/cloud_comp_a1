"""Shared HTTP helpers for simulator CLIs (stdlib only)."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

MetricsHook = Callable[[float], None]
ErrorHook = Callable[[str], None]


def request_json(
    base_url: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = 60.0,
    on_latency: MetricsHook | None = None,
    on_error: ErrorHook | None = None,
    debug_http: bool = False,
    quiet_http_statuses: set[int] | None = None,
) -> tuple[int, bytes]:
    """
    Perform HTTP request. Returns (status_code, response_body).
    Connection errors use status -1.
    """
    url = base_url.rstrip("/") + path
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.status
            raw = resp.read()
        dt = time.perf_counter() - t0
        if on_latency:
            on_latency(dt)
        return code, raw
    except urllib.error.HTTPError as e:
        dt = time.perf_counter() - t0
        try:
            raw_err = e.read()
        except Exception:
            raw_err = b""
        if on_latency:
            on_latency(dt)
        is_quiet = e.code in (quiet_http_statuses or set())
        if on_error and not is_quiet:
            on_error(f"{method} {url} HTTP {e.code}")
        if not is_quiet:
            print(
                f"[http_client] HTTP {e.code} {method} {url}",
                file=sys.stderr,
                flush=True,
            )
        if debug_http:
            snippet = raw_err.decode("utf-8", errors="replace")[:4000]
            if snippet.strip():
                print(snippet, file=sys.stderr, flush=True)
        return e.code, raw_err
    except Exception as e:
        dt = time.perf_counter() - t0
        if on_latency:
            on_latency(dt)
        if on_error:
            on_error(f"{method} {url} {type(e).__name__}: {e}")
        print(
            f"[http_client] {type(e).__name__} {method} {url}: {e}",
            file=sys.stderr,
            flush=True,
        )
        return -1, b""


def json_load(raw: bytes) -> Any:
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))
