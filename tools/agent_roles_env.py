"""
Auto-provisioned ECS IAM roles for the agent (Bedrock account).

When EXECUTION_ROLE_ARN / TASK_ROLE_ARN are not set in `.env.agent`, deploy creates
roles and writes `agent_roles.env` (gitignored). Teardown deletes only those managed
roles; ARNs supplied in `.env.agent` are never deleted.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from botocore.exceptions import ClientError
from dotenv import dotenv_values, load_dotenv

from tools.agent_infra import AGENT_BEDROCK_POLICY_NAME, AGENT_SESSIONS_POLICY_NAME
from tools.ecs_infra import ensure_execution_role, ensure_task_role
from tools.state import DeploymentState

AGENT_ROLES_ENV_PATH = Path(__file__).resolve().parent.parent / "agent_roles.env"

K_EXEC_ARN = "EXECUTION_ROLE_ARN"
K_TASK_ARN = "TASK_ROLE_ARN"
K_EXEC_NAME = "EXECUTION_ROLE_NAME"
K_TASK_NAME = "TASK_ROLE_NAME"
K_MANAGED = "AGENT_ROLES_MANAGED"

_PLACEHOLDER_MARKERS = ("ACCOUNT_ID", "YourBedrock", "REPLACE_ME")


@dataclass(frozen=True)
class AgentRolesFile:
    execution_role_arn: str
    task_role_arn: str
    execution_role_name: str
    task_role_name: str
    managed: bool = True


def is_meaningful_role_arn(value: str | None) -> bool:
    v = (value or "").strip()
    if not v:
        return False
    if any(m in v for m in _PLACEHOLDER_MARKERS):
        return False
    return bool(re.match(r"^arn:aws:iam::\d{12}:role/", v))


def _parse_agent_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return {k: (v or "").strip() for k, v in dotenv_values(path).items() if k}


def roles_explicit_in_env_agent(project_root: Path | None = None) -> tuple[str, str] | None:
    """Role ARNs set only in `.env.agent` (not placeholders)."""
    root = project_root or Path(__file__).resolve().parent.parent
    vals = _parse_agent_env_file(root / ".env.agent")
    exec_arn = vals.get(K_EXEC_ARN, "")
    task_arn = vals.get(K_TASK_ARN, "")
    if is_meaningful_role_arn(exec_arn) and is_meaningful_role_arn(task_arn):
        return exec_arn, task_arn
    return None


def load_agent_roles_file(path: Path | None = None) -> AgentRolesFile | None:
    p = path or AGENT_ROLES_ENV_PATH
    if not p.is_file():
        return None
    vals = _parse_agent_env_file(p)
    exec_arn = vals.get(K_EXEC_ARN, "")
    task_arn = vals.get(K_TASK_ARN, "")
    exec_name = vals.get(K_EXEC_NAME, "")
    task_name = vals.get(K_TASK_NAME, "")
    managed = (vals.get(K_MANAGED) or "true").strip().lower() in ("1", "true", "yes")
    if not is_meaningful_role_arn(exec_arn) or not is_meaningful_role_arn(task_arn):
        return None
    if not exec_name:
        exec_name = role_name_from_arn(exec_arn)
    if not task_name:
        task_name = role_name_from_arn(task_arn)
    return AgentRolesFile(
        execution_role_arn=exec_arn,
        task_role_arn=task_arn,
        execution_role_name=exec_name,
        task_role_name=task_name,
        managed=managed,
    )


def write_agent_roles_file(
    *,
    execution_role_arn: str,
    task_role_arn: str,
    execution_role_name: str,
    task_role_name: str,
    path: Path | None = None,
) -> Path:
    p = path or AGENT_ROLES_ENV_PATH
    lines = [
        "# Auto-created by deploy_agent.py — safe to delete; recreated on next deploy.",
        "# Teardown removes these IAM roles when AGENT_ROLES_MANAGED=true.",
        f"{K_MANAGED}=true",
        f"{K_EXEC_ARN}={execution_role_arn}",
        f"{K_TASK_ARN}={task_role_arn}",
        f"{K_EXEC_NAME}={execution_role_name}",
        f"{K_TASK_NAME}={task_role_name}",
        "",
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    print(f"[deploy_agent] Wrote {p} (managed IAM roles)")
    return p


def load_agent_roles_dotenv(project_root: Path | None = None) -> None:
    """Load agent_roles.env without overriding variables already set in `.env.agent`."""
    root = project_root or Path(__file__).resolve().parent.parent
    p = root / "agent_roles.env"
    if p.is_file():
        load_dotenv(p, override=False)


def role_name_from_arn(arn: str) -> str:
    return arn.rstrip("/").split("/")[-1]


def resolve_agent_iam_roles(
    iam,
    suffix: str,
    state: DeploymentState,
    *,
    project_root: Path | None = None,
) -> tuple[str, str, str, bool]:
    """
    Resolve execution + task role ARNs and whether deploy manages them.

    Returns (execution_arn, task_arn, task_role_name, managed).
    """
    root = project_root or Path(__file__).resolve().parent.parent
    explicit = roles_explicit_in_env_agent(root)
    if explicit:
        exec_arn, task_arn = explicit
        state.execution_role_arn = exec_arn
        state.task_role_arn = task_arn
        print(
            "  [IAM] Using EXECUTION_ROLE_ARN / TASK_ROLE_ARN from .env.agent "
            "(not managed; skipped agent_roles.env)"
        )
        return exec_arn, task_arn, role_name_from_arn(task_arn), False

    saved = load_agent_roles_file(root / "agent_roles.env")
    if saved and saved.managed:
        os.environ.setdefault(K_EXEC_ARN, saved.execution_role_arn)
        os.environ.setdefault(K_TASK_ARN, saved.task_role_arn)
        state.execution_role_arn = saved.execution_role_arn
        state.task_role_arn = saved.task_role_arn
        print(f"  [IAM] Using roles from {AGENT_ROLES_ENV_PATH.name}")
        return (
            saved.execution_role_arn,
            saved.task_role_arn,
            saved.task_role_name,
            True,
        )

    # Create (or reuse by suffix) via ecs_infra helpers
    for key in (K_EXEC_ARN, K_TASK_ARN):
        os.environ.pop(key, None)

    exec_arn = ensure_execution_role(iam, suffix, state)
    task_arn = ensure_task_role(iam, suffix, state)
    exec_name = f"dijkfood-ecs-exec-{suffix}"
    task_name = f"dijkfood-ecs-task-{suffix}"
    write_agent_roles_file(
        execution_role_arn=exec_arn,
        task_role_arn=task_arn,
        execution_role_name=exec_name,
        task_role_name=task_name,
        path=root / "agent_roles.env",
    )
    return exec_arn, task_arn, task_name, True


def _detach_managed_policies(iam, role_name: str) -> None:
    try:
        attached = iam.list_attached_role_policies(RoleName=role_name).get(
            "AttachedPolicies", []
        )
    except ClientError as exc:
        print(f"  [teardown] IAM list attached {role_name}: {exc.response['Error']['Code']}")
        return
    for pol in attached:
        try:
            iam.detach_role_policy(RoleName=role_name, PolicyArn=pol["PolicyArn"])
        except ClientError as exc:
            print(
                f"  [teardown] IAM detach {role_name} {pol['PolicyArn']}: "
                f"{exc.response['Error']['Code']}"
            )

    for inline_name in (
        AGENT_SESSIONS_POLICY_NAME,
        AGENT_BEDROCK_POLICY_NAME,
    ):
        try:
            iam.delete_role_policy(RoleName=role_name, PolicyName=inline_name)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code != "NoSuchEntity":
                print(f"  [teardown] IAM delete inline {role_name}/{inline_name}: {code}")


def _delete_iam_role(iam, role_name: str) -> None:
    _detach_managed_policies(iam, role_name)
    try:
        iam.delete_role(RoleName=role_name)
        print(f"  [teardown] Deleted IAM role {role_name}")
    except ClientError as exc:
        print(f"  [teardown] IAM role {role_name}: {exc.response['Error']['Code']}")


def destroy_managed_agent_roles(iam, project_root: Path | None = None) -> None:
    """Delete IAM roles recorded in agent_roles.env (managed deploy only)."""
    root = project_root or Path(__file__).resolve().parent.parent
    path = root / "agent_roles.env"
    saved = load_agent_roles_file(path)
    if not saved or not saved.managed:
        return
    print("[deploy_agent] --- IAM (managed agent roles) ---")
    _delete_iam_role(iam, saved.execution_role_name)
    _delete_iam_role(iam, saved.task_role_name)
    try:
        path.unlink()
        print(f"[deploy_agent] Removed {path}")
    except OSError as exc:
        print(f"[deploy_agent] Could not remove {path}: {exc}")
