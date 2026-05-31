"""Read `.env.agent` for Bedrock keys and agent tuning without hijacking lab deploy credentials."""

from __future__ import annotations

import os
from pathlib import Path

import boto3
from dotenv import dotenv_values, load_dotenv

# Never inject these from `.env.agent` into os.environ (deploy.py uses lab creds).
_AGENT_CREDENTIAL_KEYS = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)
# Lab deploy region comes from `.env` / deploy.py, not the Bedrock account file.
_AGENT_REGION_KEYS = frozenset({"AWS_REGION", "AWS_DEFAULT_REGION"})


def read_agent_dotenv(project_root: Path | None = None) -> dict[str, str]:
    """Parse `.env.agent` without modifying the process environment."""
    root = project_root or Path(__file__).resolve().parent.parent
    path = root / ".env.agent"
    if not path.is_file():
        return {}
    return {
        k: (v or "").strip()
        for k, v in dotenv_values(path).items()
        if k and v is not None
    }


def restore_lab_credentials_for_deploy(project_root: Path | None = None) -> None:
    """
    After loading `.env.agent`, reset deploy credentials from `.env` (Learner Lab).

    If `.env` has no access keys, remove Bedrock keys from the environment so
    boto3 falls back to ~/.aws/credentials / instance profile.
    """
    root = project_root or Path(__file__).resolve().parent.parent
    lab_path = root / ".env"
    if not lab_path.is_file():
        for key in _AGENT_CREDENTIAL_KEYS:
            os.environ.pop(key, None)
        return
    lab_vals = {
        k: (v or "").strip()
        for k, v in dotenv_values(lab_path).items()
        if k and v is not None
    }
    for key in _AGENT_CREDENTIAL_KEYS:
        lab_val = lab_vals.get(key, "")
        if lab_val:
            os.environ[key] = lab_val
        else:
            os.environ.pop(key, None)


def load_agent_dotenv(project_root: Path | None = None) -> None:
    """
    Load agent tuning from `.env.agent` (model, tool limits, CORS, etc.).

    Does **not** set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
    or AWS_REGION so `deploy.py` keeps using Learner Lab credentials from `.env`
    or the default boto3 chain.
    """
    root = project_root or Path(__file__).resolve().parent.parent
    for key, val in read_agent_dotenv(root).items():
        if key in _AGENT_CREDENTIAL_KEYS or key in _AGENT_REGION_KEYS:
            continue
        if val:
            os.environ.setdefault(key, val)
    from tools.agent_roles_env import load_agent_roles_dotenv

    load_agent_roles_dotenv(root)


def agent_boto_session(
    region: str,
    *,
    project_root: Path | None = None,
    require_keys: bool = True,
) -> boto3.Session:
    """
    Build a boto3 session for the Bedrock/credits account (reads `.env.agent` file only).
    """
    vals = read_agent_dotenv(project_root)
    key = vals.get("AWS_ACCESS_KEY_ID", "")
    secret = vals.get("AWS_SECRET_ACCESS_KEY", "")
    token = vals.get("AWS_SESSION_TOKEN", "") or None
    bedrock_region = (
        vals.get("AWS_REGION") or vals.get("AWS_DEFAULT_REGION") or region
    ).strip()

    if not key or not secret:
        if require_keys:
            raise RuntimeError(
                "Missing AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY in .env.agent "
                "(see .env.agent.example)."
            )
        return boto3.Session(region_name=bedrock_region)

    return boto3.Session(
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        aws_session_token=token or None,
        region_name=bedrock_region,
    )
