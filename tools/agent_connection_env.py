"""
Persist agent-stack DeploymentState to agent_connection.env (Bedrock / credits account).

Separate from lab connection.env. No secrets stored.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

from tools.state import DeploymentState, EcsServiceRecord

AGENT_CONNECTION_ENV_PATH = (
    Path(__file__).resolve().parent.parent / "agent_connection.env"
)

K_REGION = "AWS_REGION"
K_SUFFIX = "DEPLOYMENT_SUFFIX"
K_AGENT_BASE_URL = "AGENT_BASE_URL"
K_LAB_BASE_URL = "LAB_BASE_URL"
K_ECS_TASK_SG = "ECS_TASK_SECURITY_GROUP_ID"
K_ALB_SG = "ALB_SECURITY_GROUP_ID"
K_ALB_ARN = "ALB_ARN"
K_LISTENER_ARN = "LISTENER_ARN"
K_CLUSTER = "ECS_CLUSTER_NAME"
K_ECS_SERVICES_JSON = "ECS_SERVICES_JSON"
K_EXEC_ROLE_ARN = "EXECUTION_ROLE_ARN"
K_TASK_ROLE_ARN = "TASK_ROLE_ARN"
K_DDB_AGENT_SESSIONS = "DYNAMO_AGENT_SESSIONS_TABLE"
K_AGENT_UI_TG_ARN = "AGENT_UI_TARGET_GROUP_ARN"
K_AGENT_UI_RULE_ARN = "AGENT_UI_LISTENER_RULE_ARN"
K_AGENT_UI_URL = "AGENT_UI_URL"
K_SG_EXTRA = "CREATED_SECURITY_GROUP_IDS"


def write_agent_connection_env(
    state: DeploymentState,
    agent_base_url: str,
    lab_base_url: str,
    region: str,
    *,
    quiet: bool = False,
) -> Path:
    abu = agent_base_url.rstrip("/") if agent_base_url else ""
    lbu = lab_base_url.rstrip("/") if lab_base_url else ""
    lines = [
        "# DijkFood agent deployment (Bedrock account) — do not commit.",
        f"{K_REGION}={region}",
        f"{K_SUFFIX}={state.suffix}",
        f"{K_AGENT_BASE_URL}={abu}",
        f"{K_LAB_BASE_URL}={lbu}",
    ]
    optional = [
        (K_ECS_TASK_SG, state.ecs_task_sg_id),
        (K_ALB_SG, state.alb_sg_id),
        (K_ALB_ARN, state.alb_arn),
        (K_LISTENER_ARN, state.listener_arn),
        (K_CLUSTER, state.cluster_name),
        (K_EXEC_ROLE_ARN, state.execution_role_arn),
        (K_TASK_ROLE_ARN, state.task_role_arn),
        (K_DDB_AGENT_SESSIONS, state.dynamo_agent_sessions_table),
        (K_AGENT_UI_TG_ARN, state.agent_ui_target_group_arn),
        (K_AGENT_UI_RULE_ARN, state.agent_ui_listener_rule_arn),
        (K_AGENT_UI_URL, state.agent_ui_url),
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
    AGENT_CONNECTION_ENV_PATH.write_text("\n".join(lines), encoding="utf-8")
    if not quiet:
        print(f"[deploy_agent] Wrote {AGENT_CONNECTION_ENV_PATH}")
    return AGENT_CONNECTION_ENV_PATH


def load_agent_connection_env(
    path: Path | None = None,
    *,
    require_ecs_services_when_cluster_set: bool = True,
) -> tuple[DeploymentState, str, str, str]:
    p = path or AGENT_CONNECTION_ENV_PATH
    if not p.is_file():
        raise FileNotFoundError(
            f"Missing {p}; run python deploy_agent.py first."
        )

    load_dotenv(p, override=True)

    def req(key: str) -> str:
        val = (os.getenv(key) or "").strip()
        if not val:
            raise ValueError(f"{p}: missing or empty {key}")
        return val

    region = req(K_REGION)
    suffix = req(K_SUFFIX)
    agent_base_url = (os.getenv(K_AGENT_BASE_URL) or "").strip()
    lab_base_url = (os.getenv(K_LAB_BASE_URL) or "").strip()

    state = DeploymentState(suffix=suffix)
    state.ecs_task_sg_id = (os.getenv(K_ECS_TASK_SG) or "").strip() or None
    state.alb_sg_id = (os.getenv(K_ALB_SG) or "").strip() or None
    state.alb_arn = (os.getenv(K_ALB_ARN) or "").strip() or None
    state.listener_arn = (os.getenv(K_LISTENER_ARN) or "").strip() or None
    state.cluster_name = (os.getenv(K_CLUSTER) or "").strip() or None
    raw_svcs = (os.getenv(K_ECS_SERVICES_JSON) or "").strip()
    if (
        require_ecs_services_when_cluster_set
        and state.cluster_name
        and not raw_svcs
    ):
        raise ValueError(
            f"{p}: {K_ECS_SERVICES_JSON} is required when {K_CLUSTER} is set"
        )
    state.execution_role_arn = (os.getenv(K_EXEC_ROLE_ARN) or "").strip() or None
    state.task_role_arn = (os.getenv(K_TASK_ROLE_ARN) or "").strip() or None
    state.dynamo_agent_sessions_table = (
        os.getenv(K_DDB_AGENT_SESSIONS) or ""
    ).strip() or None
    state.agent_ui_target_group_arn = (
        os.getenv(K_AGENT_UI_TG_ARN) or ""
    ).strip() or None
    state.agent_ui_listener_rule_arn = (
        os.getenv(K_AGENT_UI_RULE_ARN) or ""
    ).strip() or None
    state.agent_ui_url = (os.getenv(K_AGENT_UI_URL) or "").strip() or None
    if raw_svcs:
        try:
            for item in json.loads(raw_svcs):
                state.ecs_services.append(EcsServiceRecord(**item))
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"{p}: invalid {K_ECS_SERVICES_JSON}: {e}") from e
    extra = (os.getenv(K_SG_EXTRA) or "").strip()
    if extra:
        for sg in extra.split(","):
            sg = sg.strip()
            if sg:
                state.note_sg(sg)

    return state, region, agent_base_url, lab_base_url
