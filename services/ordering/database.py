"""PostgreSQL connection for FastAPI dependencies."""

from __future__ import annotations

import os
from collections.abc import Generator

import psycopg
from psycopg.rows import dict_row


def _conninfo() -> str:
    return (
        f"host={os.environ.get('DB_HOST', '')} "
        f"port={os.environ.get('DB_PORT', '5432')} "
        f"dbname={os.environ.get('DB_NAME', '')} "
        f"user={os.environ.get('DB_USER', '')} "
        f"password={os.environ.get('DB_PASSWORD', '')} "
        "connect_timeout=10"
    )


def get_db() -> Generator[psycopg.Connection, None, None]:
    conn = psycopg.connect(_conninfo(), row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
