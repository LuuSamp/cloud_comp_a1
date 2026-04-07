"""
DijkFood API: health checks plus CRUD routers per entity (RDS + DynamoDB).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import boto3
import psycopg
from botocore.exceptions import ClientError
from fastapi import FastAPI

from routers import (
    courier_positions,
    couriers,
    customers,
    food_places,
    order_logs,
    order_statuses,
    orders,
    sim_placeholders,
)

app = FastAPI(title="DijkFood API")

app.include_router(customers.router)
app.include_router(food_places.router)
app.include_router(couriers.router)
app.include_router(order_statuses.router)
app.include_router(orders.router)
app.include_router(order_logs.router)
app.include_router(courier_positions.router)
app.include_router(sim_placeholders.router)


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


@app.get("/dynamo-check")
def dynamo_check() -> dict[str, str]:
    logs_table = os.environ.get("DYNAMODB_ORDER_LOGS_TABLE", "")
    pos_table = os.environ.get("DYNAMODB_COURIER_POSITIONS_TABLE", "")
    if not logs_table or not pos_table:
        return {"dynamo": "skipped", "detail": "DynamoDB table env vars unset"}
    region = os.environ.get("AWS_REGION") or os.environ.get(
        "AWS_DEFAULT_REGION", "us-east-1"
    )
    ddb = boto3.client("dynamodb", region_name=region)
    try:
        ddb.describe_table(TableName=logs_table)
        ddb.describe_table(TableName=pos_table)
    except ClientError as e:
        return {"dynamo": "error", "detail": str(e)}
    return {"dynamo": "ok"}
