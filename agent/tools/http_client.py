"""HTTP client for ordering, tracking, and routing microservices."""

from __future__ import annotations

import os
from typing import Any

import httpx

_CONNECT_TIMEOUT_S = float(os.environ.get("AGENT_CONNECT_TIMEOUT_S", "5.0"))
_DEFAULT_TIMEOUT_S = float(os.environ.get("AGENT_REQUEST_TIMEOUT_S", "30.0"))
_LIMITS = httpx.Limits(
    max_connections=int(os.environ.get("AGENT_MAX_CONNECTIONS", "50")),
    max_keepalive_connections=int(os.environ.get("AGENT_MAX_KEEPALIVE_CONNECTIONS", "10")),
)
_CLIENT = httpx.Client(
    timeout=httpx.Timeout(_DEFAULT_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S),
    limits=_LIMITS,
)


def ordering_base_url() -> str:
    base = (os.environ.get("BASE_URL") or os.environ.get("ORDERING_BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise ValueError("BASE_URL (or ORDERING_BASE_URL) is not set")
    return base


def tracking_base_url() -> str:
    explicit = (os.environ.get("TRACKING_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    return f"{ordering_base_url()}/tracking"


def routing_base_url() -> str:
    base = (os.environ.get("ROUTING_BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise ValueError("ROUTING_BASE_URL is not set")
    return base


def get_json(
    base: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout_s: float | None = None,
) -> tuple[int, Any]:
    """Return (status_code, parsed_json_or_text)."""
    url = f"{base.rstrip('/')}{path}"
    timeout = httpx.Timeout(
        timeout_s or _DEFAULT_TIMEOUT_S,
        connect=_CONNECT_TIMEOUT_S,
    )
    try:
        r = _CLIENT.get(url, params=params, timeout=timeout)
    except httpx.TimeoutException:
        return 0, {"detail": "timeout"}
    except httpx.HTTPError as exc:
        return 0, {"detail": str(exc)}
    try:
        body: Any = r.json()
    except Exception:
        body = r.text
    return r.status_code, body


def normalize_http_result(status_code: int, body: Any, *, tool: str) -> dict[str, Any]:
    if status_code == 0:
        err = body.get("detail", "timeout") if isinstance(body, dict) else "transport_error"
        return {
            "ok": False,
            "error": "timeout" if err == "timeout" else "transport_error",
            "tool": tool,
            "detail": str(err),
        }
    if status_code == 404:
        return {"ok": False, "error": "not_found", "tool": tool, "detail": body}
    if status_code in (501, 503):
        return {
            "ok": False,
            "error": "service_unavailable",
            "tool": tool,
            "retryable": True,
            "status_code": status_code,
            "detail": body,
        }
    if status_code == 202:
        return {"ok": True, "status": "pending", "tool": tool, "data": body}
    if status_code >= 400:
        return {
            "ok": False,
            "error": "http_error",
            "tool": tool,
            "status_code": status_code,
            "detail": body,
        }
    return {"ok": True, "tool": tool, "data": body}
