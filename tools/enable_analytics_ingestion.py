"""Enable analytics ingestion on deployed DynamoDB tables.

Delegates to tools.analytics_infra (also invoked by deploy.py --with-analytics).
"""
from __future__ import annotations

import os
from pathlib import Path

import boto3
from dotenv import dotenv_values, load_dotenv

from tools.analytics_infra import enable_analytics_ingestion
from tools.connection_env import load_connection_env
from tools.glue_infra import ensure_events_table, run_glue_crawler
from tools.state import DeploymentState


def _resolved_env() -> dict[str, str]:
    values: dict[str, str] = {}
    values.update({k: v for k, v in dotenv_values(Path.cwd() / ".env").items() if v})
    connection_path = Path.cwd() / "connection.env"
    if connection_path.is_file():
        values.update({k: v for k, v in dotenv_values(connection_path).items() if v})
    return values


def main() -> None:
    load_dotenv(Path.cwd() / ".env", override=True)
    if (Path.cwd() / "connection.env").is_file():
        state, region, base_url = load_connection_env()
    else:
        env = _resolved_env()
        region = env.get("AWS_REGION", "us-east-1")
        state = DeploymentState(suffix=env.get("DEPLOYMENT_SUFFIX", "manual"))
        state.datalake_s3_bucket = env.get("DATALAKE_S3_BUCKET")
        state.dynamo_order_logs_table = env.get("DYNAMO_ORDER_LOGS_TABLE")
        state.dynamo_courier_positions_table = env.get("DYNAMO_COURIER_POSITIONS_TABLE")
        state.glue_crawler_name = env.get("GLUE_CRAWLER_NAME")
        state.glue_database = env.get("GLUE_DATABASE")
        base_url = env.get("BASE_URL", "")
    ordering_base_url = (base_url or os.environ.get("BASE_URL") or "").strip()
    if not ordering_base_url:
        print("Set BASE_URL in connection.env")
        return
    session = boto3.Session(region_name=region)
    enable_analytics_ingestion(
        ddb=session.client("dynamodb"),
        lambda_client=session.client("lambda"),
        state=state,
        ordering_base_url=ordering_base_url,
    )
    glue = session.client("glue")
    if state.glue_database and state.datalake_s3_bucket:
        ensure_events_table(
            glue,
            db_name=state.glue_database,
            datalake_bucket=state.datalake_s3_bucket,
        )
    if state.glue_crawler_name:
        run_glue_crawler(glue, state.glue_crawler_name, wait=False)
    print("Analytics ingestion enabled.")


if __name__ == "__main__":
    main()
