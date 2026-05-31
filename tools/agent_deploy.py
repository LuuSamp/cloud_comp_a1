"""Lab-hosted agent ECS spec and task environment (Bedrock via BEDROCK_* keys only)."""

from __future__ import annotations

import os
from pathlib import Path

from tools.agent_aws import read_agent_dotenv
from tools.agent_infra import default_bedrock_model_id

AGENT_SERVICE_ID = "agent"


def require_bedrock_credentials(project_root: Path | None = None) -> None:
    """Fail fast when --with-agent but `.env.agent` lacks Bedrock API keys."""
    vals = read_agent_dotenv(project_root)
    if not vals.get("AWS_ACCESS_KEY_ID") or not vals.get("AWS_SECRET_ACCESS_KEY"):
        raise RuntimeError(
            "Missing AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY in .env.agent "
            "(required for --with-agent). See .env.agent.example."
        )


def bedrock_task_env_from_agent_file(
    project_root: Path | None = None,
) -> list[dict[str, str]]:
    """
    Map `.env.agent` credentials to BEDROCK_* task vars so the lab task role
    remains the default chain for DynamoDB (sessions / usage).
    """
    vals = read_agent_dotenv(project_root)
    key = vals.get("AWS_ACCESS_KEY_ID", "")
    secret = vals.get("AWS_SECRET_ACCESS_KEY", "")
    token = vals.get("AWS_SESSION_TOKEN", "")
    region = (
        vals.get("AWS_REGION")
        or vals.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    ).strip()
    env: list[dict[str, str]] = []
    if key:
        env.append({"name": "BEDROCK_AWS_ACCESS_KEY_ID", "value": key})
    if secret:
        env.append({"name": "BEDROCK_AWS_SECRET_ACCESS_KEY", "value": secret})
    if token:
        env.append({"name": "BEDROCK_AWS_SESSION_TOKEN", "value": token})
    env.append({"name": "BEDROCK_AWS_REGION", "value": region})
    return env


def _agent_config(
    project_root: Path | None,
    key: str,
    default: str = "",
) -> str:
    """Agent setting: os.environ (from load_agent_dotenv) then `.env.agent` file."""
    env_val = (os.environ.get(key) or "").strip()
    if env_val:
        return env_val
    return read_agent_dotenv(project_root).get(key, default).strip()


def agent_service_spec(project_root: Path) -> dict[str, object]:
    return {
        "id": AGENT_SERVICE_ID,
        "ecr_suffix": "agent",
        "docker_context": project_root,
        "dockerfile": project_root / "agent" / "Dockerfile",
        "path_pattern": "/agent*",
        "cpu": "256",
        "memory": "512",
        "desired_count": int(_agent_config(project_root, "AGENT_DESIRED_COUNT", "1") or "1"),
        "full_stack_env": False,
        "autoscaling": {
            "enabled": True,
            "min_capacity": int(_agent_config(project_root, "AGENT_ASG_MIN", "1") or "1"),
            "max_capacity": int(_agent_config(project_root, "AGENT_ASG_MAX", "4") or "4"),
            "cpu_target": 50.0,
            "memory_target": 50.0,
            "scale_in_cooldown": 120,
            "scale_out_cooldown": 45,
        },
    }


def build_agent_task_environment(
    *,
    region: str,
    alb_base_url: str,
    sessions_table: str,
    model_id: str | None = None,
    project_root: Path | None = None,
) -> list[dict[str, str]]:
    """Task env: lab ALB for microservices; BEDROCK_* for Converse; lab role for DynamoDB."""
    base = alb_base_url.rstrip("/")
    mid = (
        model_id
        or _agent_config(project_root, "BEDROCK_MODEL_ID")
        or default_bedrock_model_id()
    ).strip()
    max_rounds = _agent_config(project_root, "AGENT_MAX_TOOL_ROUNDS", "5") or "5"
    env: list[dict[str, str]] = [
        {"name": "AWS_REGION", "value": region},
        {"name": "AWS_DEFAULT_REGION", "value": region},
        {"name": "SERVICE_ID", "value": AGENT_SERVICE_ID},
        {"name": "BASE_URL", "value": base},
        {"name": "TRACKING_BASE_URL", "value": f"{base}/tracking"},
        {"name": "ROUTING_BASE_URL", "value": base},
        {
            "name": "DYNAMODB_AGENT_SESSIONS_TABLE",
            "value": sessions_table,
        },
        {"name": "BEDROCK_MODEL_ID", "value": mid},
        {"name": "AGENT_MAX_TOOL_ROUNDS", "value": max_rounds},
    ]
    env.extend(bedrock_task_env_from_agent_file(project_root))
    cors = _agent_config(project_root, "AGENT_CORS_ORIGINS")
    if cors:
        env.append({"name": "AGENT_CORS_ORIGINS", "value": cors})
    enabled = _agent_config(project_root, "AGENT_ENABLED_TOOLS")
    if enabled:
        env.append({"name": "AGENT_ENABLED_TOOLS", "value": enabled})
    disabled = _agent_config(project_root, "AGENT_DISABLED_TOOLS")
    if disabled:
        env.append({"name": "AGENT_DISABLED_TOOLS", "value": disabled})
    for key in (
        "AGENT_MAX_OUTPUT_TOKENS",
        "AGENT_USAGE_BUDGET_TOKENS",
        "AGENT_USAGE_DAILY_BUDGET_TOKENS",
    ):
        val = _agent_config(project_root, key)
        if val:
            env.append({"name": key, "value": val})
    return env
