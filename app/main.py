"""
Minimal DijkFood API: health check and DB connectivity probe for ECS/ALB.
"""

from __future__ import annotations

import os

import psycopg
from fastapi import FastAPI

app = FastAPI(title="DijkFood API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/db-check")
def db_check() -> dict[str, str]:
    host = os.environ.get("DB_HOST", "")
    if not host:
        return {"db": "skipped", "detail": "DB_HOST unset"}
    conn_str = (
        f"host={host} port={os.environ.get('DB_PORT', '5432')} "
        f"dbname={os.environ.get('DB_NAME', '')} "
        f"user={os.environ.get('DB_USER', '')} "
        f"password={os.environ.get('DB_PASSWORD', '')} "
        "connect_timeout=5"
    )
    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    return {"db": "ok"}
