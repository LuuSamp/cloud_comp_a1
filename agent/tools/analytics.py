"""Historical analytics via Athena (Glue catalog)."""

from __future__ import annotations

import os
import time
from typing import Any

import boto3

from agent.tools.registry import ToolSpec, object_schema, register_tool

REGION = os.environ.get("AWS_REGION", "us-east-1")
ATHENA_DB = (os.environ.get("GLUE_DATABASE") or os.environ.get("ATHENA_DB") or "").strip()
DATALAKE_BUCKET = (os.environ.get("DATALAKE_S3_BUCKET") or "").strip()


def _run_athena_query(sql: str) -> dict[str, Any]:
    if not ATHENA_DB:
        return {"ok": False, "error": "athena_not_configured", "detail": "Set GLUE_DATABASE on agent task"}
    if not DATALAKE_BUCKET:
        return {"ok": False, "error": "datalake_not_configured"}
    staging = f"s3://{DATALAKE_BUCKET}/athena-results/"
    client = boto3.client("athena", region_name=REGION)
    resp = client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": ATHENA_DB},
        ResultConfiguration={"OutputLocation": staging},
    )
    qid = resp["QueryExecutionId"]
    for _ in range(30):
        status = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"]
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "CANCELLED"):
            reason = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"].get(
                "StateChangeReason", ""
            )
            return {"ok": False, "error": "athena_failed", "detail": reason}
        time.sleep(1)
    else:
        return {"ok": False, "error": "athena_timeout"}

    results = client.get_query_results(QueryExecutionId=qid)
    rows = results.get("ResultSet", {}).get("Rows", [])
    if not rows:
        return {"ok": True, "tool": "query_analytics", "rows": []}
    headers = [c.get("VarCharValue", "") for c in rows[0].get("Data", [])]
    out = []
    for row in rows[1:]:
        vals = [c.get("VarCharValue") for c in row.get("Data", [])]
        out.append(dict(zip(headers, vals)))
    return {"ok": True, "tool": "query_analytics", "rows": out}


def _query_analytics(args: dict[str, Any]) -> dict[str, Any]:
    sql = (args.get("sql") or "").strip()
    if not sql:
        return {"ok": False, "error": "missing_sql"}
    if "limit" not in sql.lower():
        sql = f"{sql.rstrip(';')} LIMIT 100"
    return _run_athena_query(sql)


def register_analytics_tools() -> None:
    register_tool(
        ToolSpec(
            name="query_analytics",
            description=(
                "Run a read-only SQL query on historical operational events in Athena/Glue. "
                "Table name is typically 'events'. Use for aggregates over order history."
            ),
            input_schema=object_schema(
                {
                    "sql": {
                        "type": "string",
                        "description": "SELECT query (LIMIT added automatically if missing)",
                    }
                },
                required=["sql"],
            ),
            handler=_query_analytics,
            service="agent",
            status="beta",
            endpoint_ref="athena",
        )
    )
