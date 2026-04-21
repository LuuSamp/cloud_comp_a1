"""
Persist DeploymentState to connection.env for teardown-only / load-test-only.

No secrets (e.g. DB password) are written.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

from tools.state import DeploymentState, EcsServiceRecord

CONNECTION_ENV_PATH = Path(__file__).resolve().parent.parent / "connection.env"

# Keys (stable; document in README)
K_REGION = "AWS_REGION"
K_SUFFIX = "DEPLOYMENT_SUFFIX"
K_BASE_URL = "BASE_URL"
K_RDS_INSTANCE = "RDS_INSTANCE_ID"
K_DB_HOST = "DB_HOST"
K_RDS_SG = "RDS_SECURITY_GROUP_ID"
K_DB_SUBNET = "DB_SUBNET_GROUP"
K_ECS_TASK_SG = "ECS_TASK_SECURITY_GROUP_ID"
K_ALB_SG = "ALB_SECURITY_GROUP_ID"
K_ALB_ARN = "ALB_ARN"
K_LISTENER_ARN = "LISTENER_ARN"
K_CLUSTER = "ECS_CLUSTER_NAME"
K_ECS_SERVICES_JSON = "ECS_SERVICES_JSON"
K_EXEC_ROLE_ARN = "EXECUTION_ROLE_ARN"
K_TASK_ROLE_ARN = "TASK_ROLE_ARN"
K_DDB_LOGS = "DYNAMO_ORDER_LOGS_TABLE"
K_DDB_POS = "DYNAMO_COURIER_POSITIONS_TABLE"
K_DDB_ROUTES = "DYNAMO_ROUTES_TABLE"
K_ROUTING_GRAPH_S3 = "ROUTING_GRAPH_S3_BUCKET"
K_SG_EXTRA = "CREATED_SECURITY_GROUP_IDS"


def write_connection_env(
    state: DeploymentState,
    base_url: str,
    region: str,
    *,
    quiet: bool = False,
) -> Path:
    bu = base_url.rstrip("/") if base_url else ""
    lines = [
        "# DijkFood deployment snapshot — do not commit; no secrets stored.",
        f"{K_REGION}={region}",
        f"{K_SUFFIX}={state.suffix}",
        f"{K_BASE_URL}={bu}",
    ]
    optional = [
        (K_RDS_INSTANCE, state.rds_instance_id),
        (K_DB_HOST, state.rds_endpoint),
        (K_RDS_SG, state.rds_sg_id),
        (K_DB_SUBNET, state.db_subnet_group),
        (K_ECS_TASK_SG, state.ecs_task_sg_id),
        (K_ALB_SG, state.alb_sg_id),
        (K_ALB_ARN, state.alb_arn),
        (K_LISTENER_ARN, state.listener_arn),
        (K_CLUSTER, state.cluster_name),
        (K_EXEC_ROLE_ARN, state.execution_role_arn),
        (K_TASK_ROLE_ARN, state.task_role_arn),
        (K_DDB_LOGS, state.dynamo_order_logs_table),
        (K_DDB_POS, state.dynamo_courier_positions_table),
        (K_DDB_ROUTES, state.dynamo_routes_table),
        (K_ROUTING_GRAPH_S3, state.routing_graph_s3_bucket),
    ]
    for k, v in optional:
        if v:
            lines.append(f"{k}={v}")
    if state.ecs_services:
        payload = json.dumps(
            [asdict(s) for s in state.ecs_services],
            separators=(",", ":"),
        )
        lines.append(f"{K_ECS_SERVICES_JSON}={payload}")
    if state.created_sg_ids:
        lines.append(f"{K_SG_EXTRA}={','.join(state.created_sg_ids)}")
    lines.append("")
    CONNECTION_ENV_PATH.write_text("\n".join(lines), encoding="utf-8")
    if not quiet:
        print(f"[deploy] Wrote {CONNECTION_ENV_PATH}")
    return CONNECTION_ENV_PATH


def load_connection_env(
    path: Path | None = None,
    *,
    require_ecs_services_when_cluster_set: bool = True,
) -> tuple[DeploymentState, str, str]:
    p = path or CONNECTION_ENV_PATH
    load_dotenv(CONNECTION_ENV_PATH.parent / ".env")
    if not p.is_file():
        raise FileNotFoundError(f"Missing {p}; run deploy with --skip-teardown first.")

    load_dotenv(p, override=True)

    def req(key: str) -> str:
        val = (os.getenv(key) or "").strip()
        if not val:
            raise ValueError(f"{p}: missing or empty {key}")
        return val

    region = req(K_REGION)
    suffix = req(K_SUFFIX)
    base_url = (os.getenv(K_BASE_URL) or "").strip()

    state = DeploymentState(suffix=suffix)
    state.rds_instance_id = (os.getenv(K_RDS_INSTANCE) or "").strip() or None
    state.rds_endpoint = (os.getenv(K_DB_HOST) or "").strip() or None
    state.rds_sg_id = (os.getenv(K_RDS_SG) or "").strip() or None
    state.db_subnet_group = (os.getenv(K_DB_SUBNET) or "").strip() or None
    state.ecs_task_sg_id = (os.getenv(K_ECS_TASK_SG) or "").strip() or None
    state.alb_sg_id = (os.getenv(K_ALB_SG) or "").strip() or None
    state.alb_arn = (os.getenv(K_ALB_ARN) or "").strip() or None
    state.listener_arn = (os.getenv(K_LISTENER_ARN) or "").strip() or None
    state.cluster_name = (os.getenv(K_CLUSTER) or "").strip() or None
    raw_svcs_early = (os.getenv(K_ECS_SERVICES_JSON) or "").strip()
    if (
        require_ecs_services_when_cluster_set
        and state.cluster_name
        and not raw_svcs_early
    ):
        raise ValueError(
            f"{p}: {K_ECS_SERVICES_JSON} is required when {K_CLUSTER} is set "
            "(re-run deploy to regenerate connection.env)"
        )
    state.execution_role_arn = (os.getenv(K_EXEC_ROLE_ARN) or "").strip() or None
    state.task_role_arn = (os.getenv(K_TASK_ROLE_ARN) or "").strip() or None
    state.dynamo_order_logs_table = (os.getenv(K_DDB_LOGS) or "").strip() or None
    state.dynamo_courier_positions_table = (os.getenv(K_DDB_POS) or "").strip() or None
    state.dynamo_routes_table = (os.getenv(K_DDB_ROUTES) or "").strip() or None
    state.routing_graph_s3_bucket = (os.getenv(K_ROUTING_GRAPH_S3) or "").strip() or None
    if raw_svcs_early:
        try:
            for item in json.loads(raw_svcs_early):
                state.ecs_services.append(EcsServiceRecord(**item))
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"{p}: invalid {K_ECS_SERVICES_JSON}: {e}") from e
    extra = (os.getenv(K_SG_EXTRA) or "").strip()
    if extra:
        for sg in extra.split(","):
            sg = sg.strip()
            if sg:
                state.note_sg(sg)

    return state, region, base_url
