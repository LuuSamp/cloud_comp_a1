"""Shared boto3 clients for Bedrock account credentials."""

from __future__ import annotations

import os
from typing import Any

import boto3


def bedrock_aws_region() -> str:
    return (
        (os.environ.get("BEDROCK_AWS_REGION") or "").strip()
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    )


def _bedrock_session() -> boto3.Session | None:
    key = (
        os.environ.get("BEDROCK_AWS_ACCESS_KEY_ID")
        or os.environ.get("AWS_ACCESS_KEY_ID")
        or ""
    ).strip()
    secret = (
        os.environ.get("BEDROCK_AWS_SECRET_ACCESS_KEY")
        or os.environ.get("AWS_SECRET_ACCESS_KEY")
        or ""
    ).strip()
    token = (
        os.environ.get("BEDROCK_AWS_SESSION_TOKEN")
        or os.environ.get("AWS_SESSION_TOKEN")
        or ""
    ).strip() or None
    if key and secret:
        return boto3.Session(
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            aws_session_token=token,
            region_name=bedrock_aws_region(),
        )
    return None


def bedrock_runtime_client() -> Any:
    session = _bedrock_session()
    region = bedrock_aws_region()
    if session:
        return session.client("bedrock-runtime")
    return boto3.client("bedrock-runtime", region_name=region)


def cloudwatch_client() -> Any:
    session = _bedrock_session()
    region = bedrock_aws_region()
    if session:
        return session.client("cloudwatch")
    return boto3.client("cloudwatch", region_name=region)
