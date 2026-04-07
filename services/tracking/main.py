"""DijkFood tracking service (stub). ALB path prefix: /tracking"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="DijkFood tracking")


@app.get("/health")
def alb_health() -> dict[str, str]:
    """Target group health check (path /health at container root)."""
    return {"status": "ok", "service": "tracking"}


stub = FastAPI()

@stub.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "tracking"}

@stub.get("/")
def root() -> dict[str, str]:
    return {"service": "tracking", "detail": "stub"}

app.mount("/tracking", stub)
