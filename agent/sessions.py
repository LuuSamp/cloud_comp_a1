"""DynamoDB-backed conversation sessions for the agent API."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import uuid
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def _table_name() -> str:
    name = (os.environ.get("DYNAMODB_AGENT_SESSIONS_TABLE") or "").strip()
    if not name:
        raise RuntimeError("DYNAMODB_AGENT_SESSIONS_TABLE is not configured")
    return name


def _max_messages() -> int:
    return max(4, int(os.environ.get("AGENT_SESSION_MAX_MESSAGES", "40")))


def _table() -> Any:
    return boto3.resource("dynamodb", region_name=_region()).Table(_table_name())


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def new_conversation_id() -> str:
    return str(uuid.uuid4())


def load_messages(conversation_id: str) -> list[dict[str, Any]]:
    try:
        resp = _table().get_item(Key={"conversationId": conversation_id})
    except ClientError as exc:
        log.warning("session load failed id=%s: %s", conversation_id, exc)
        raise RuntimeError("Failed to load conversation session") from exc
    item = resp.get("Item")
    if not item:
        return []
    raw = item.get("messages")
    if isinstance(raw, str):
        try:
            messages = json.loads(raw)
        except json.JSONDecodeError:
            return []
    elif isinstance(raw, list):
        messages = raw
    else:
        return []
    return _json_safe(messages)


def save_messages(conversation_id: str, messages: list[dict[str, Any]]) -> None:
    trimmed = messages[-_max_messages() :]
    payload = json.dumps(trimmed, default=str)
    try:
        _table().put_item(
            Item={
                "conversationId": conversation_id,
                "messages": payload,
                "updatedAt": _iso_now(),
            }
        )
    except ClientError as exc:
        log.warning("session save failed id=%s: %s", conversation_id, exc)
        raise RuntimeError("Failed to save conversation session") from exc


def delete_session(conversation_id: str) -> bool:
    try:
        _table().delete_item(Key={"conversationId": conversation_id})
        return True
    except ClientError as exc:
        log.warning("session delete failed id=%s: %s", conversation_id, exc)
        return False
