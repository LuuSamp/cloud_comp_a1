"""
Persist DeploymentState to connection.env for teardown-only / load-test-only.

No secrets (e.g. DB password) are written.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from tools.state import DeploymentState

CONNECTION_ENV_PATH = Path(__file__).resolve().parent.parent / "connection.env"

# Keys (stable; document in README)
K_REGION = "AWS_REGION"
K_SUFFIX = "DEPLOYMENT_SUFFIX"
K_BASE_URL = "BASE_URL"
K_RDS_INSTANCE = "RDS_INSTANCE_ID"
K_RDS_SG = "RDS_SECURITY_GROUP_ID"
K_DB_SUBNET = "DB_SUBNET_GROUP"
K_ECS_TASK_SG = "ECS_TASK_SECURITY_GROUP_ID"
K_ALB_SG = "ALB_SECURITY_GROUP_ID"
K_ALB_ARN = "ALB_ARN"
K_LISTENER_ARN = "LISTENER_ARN"
K_TARGET_GROUP_ARN = "TARGET_GROUP_ARN"
K_CLUSTER = "ECS_CLUSTER_NAME"
K_SERVICE = "ECS_SERVICE_NAME"
K_ECR_REPO = "ECR_REPO_NAME"
K_EXEC_ROLE_ARN = "EXECUTION_ROLE_ARN"
K_TASK_ROLE_ARN = "TASK_ROLE_ARN"
K_LOG_GROUP = "LOG_GROUP_NAME"
K_TASK_FAMILY = "TASK_DEFINITION_FAMILY"
K_DDB_LOGS = "DYNAMO_ORDER_LOGS_TABLE"
K_DDB_POS = "DYNAMO_COURIER_POSITIONS_TABLE"
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
        (K_RDS_SG, state.rds_sg_id),
        (K_DB_SUBNET, state.db_subnet_group),
        (K_ECS_TASK_SG, state.ecs_task_sg_id),
        (K_ALB_SG, state.alb_sg_id),
        (K_ALB_ARN, state.alb_arn),
        (K_LISTENER_ARN, state.listener_arn),
        (K_TARGET_GROUP_ARN, state.target_group_arn),
        (K_CLUSTER, state.cluster_name),
        (K_SERVICE, state.service_name),
        (K_ECR_REPO, state.ecr_repo_name),
        (K_EXEC_ROLE_ARN, state.execution_role_arn),
        (K_TASK_ROLE_ARN, state.task_role_arn),
        (K_LOG_GROUP, state.log_group_name),
        (K_TASK_FAMILY, state.task_definition_family),
        (K_DDB_LOGS, state.dynamo_order_logs_table),
        (K_DDB_POS, state.dynamo_courier_positions_table),
    ]
    for k, v in optional:
        if v:
            lines.append(f"{k}={v}")
    if state.created_sg_ids:
        lines.append(f"{K_SG_EXTRA}={','.join(state.created_sg_ids)}")
    lines.append("")
    CONNECTION_ENV_PATH.write_text("\n".join(lines), encoding="utf-8")
    if not quiet:
        print(f"[deploy] Wrote {CONNECTION_ENV_PATH}")
    return CONNECTION_ENV_PATH


def load_connection_env(
    path: Path | None = None,
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
    state.rds_sg_id = (os.getenv(K_RDS_SG) or "").strip() or None
    state.db_subnet_group = (os.getenv(K_DB_SUBNET) or "").strip() or None
    state.ecs_task_sg_id = (os.getenv(K_ECS_TASK_SG) or "").strip() or None
    state.alb_sg_id = (os.getenv(K_ALB_SG) or "").strip() or None
    state.alb_arn = (os.getenv(K_ALB_ARN) or "").strip() or None
    state.listener_arn = (os.getenv(K_LISTENER_ARN) or "").strip() or None
    state.target_group_arn = (os.getenv(K_TARGET_GROUP_ARN) or "").strip() or None
    state.cluster_name = (os.getenv(K_CLUSTER) or "").strip() or None
    state.service_name = (os.getenv(K_SERVICE) or "").strip() or None
    state.ecr_repo_name = (os.getenv(K_ECR_REPO) or "").strip() or None
    state.execution_role_arn = (os.getenv(K_EXEC_ROLE_ARN) or "").strip() or None
    state.task_role_arn = (os.getenv(K_TASK_ROLE_ARN) or "").strip() or None
    state.log_group_name = (os.getenv(K_LOG_GROUP) or "").strip() or None
    state.task_definition_family = (os.getenv(K_TASK_FAMILY) or "").strip() or None
    state.dynamo_order_logs_table = (os.getenv(K_DDB_LOGS) or "").strip() or None
    state.dynamo_courier_positions_table = (os.getenv(K_DDB_POS) or "").strip() or None
    extra = (os.getenv(K_SG_EXTRA) or "").strip()
    if extra:
        for sg in extra.split(","):
            sg = sg.strip()
            if sg:
                state.note_sg(sg)

    return state, region, base_url
