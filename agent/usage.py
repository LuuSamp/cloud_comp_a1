"""Aggregate Bedrock usage counters in the agent sessions DynamoDB table."""

from __future__ import annotations

import datetime as dt
import logging
import os
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

from agent.sessions import _iso_now, _json_safe, _region, _table

log = logging.getLogger(__name__)

USAGE_GLOBAL_KEY = "__usage__global__"
USAGE_DAY_PREFIX = "__usage__day__"


def _budget_total() -> int | None:
    raw = (os.environ.get("AGENT_USAGE_BUDGET_TOKENS") or "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def _budget_daily() -> int | None:
    raw = (os.environ.get("AGENT_USAGE_DAILY_BUDGET_TOKENS") or "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def _day_key(day: dt.date | None = None) -> str:
    d = day or dt.datetime.now(dt.timezone.utc).date()
    return f"{USAGE_DAY_PREFIX}{d.isoformat()}__"


def _day_keys(days: int = 7) -> list[str]:
    today = dt.datetime.now(dt.timezone.utc).date()
    return [_day_key(today - dt.timedelta(days=i)) for i in range(days)]


def _to_int(item: dict[str, Any] | None, name: str) -> int:
    if not item:
        return 0
    val = item.get(name, 0)
    if isinstance(val, Decimal):
        return int(val)
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _counters(item: dict[str, Any] | None) -> dict[str, int]:
    return {
        "input_tokens": _to_int(item, "totalInputTokens"),
        "output_tokens": _to_int(item, "totalOutputTokens"),
        "total_tokens": _to_int(item, "totalTokens"),
        "request_count": _to_int(item, "requestCount"),
        "bedrock_rounds": _to_int(item, "bedrockCallCount"),
        "tool_calls": _to_int(item, "toolCallCount"),
    }


def record_chat_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    bedrock_rounds: int,
    tool_calls: int,
) -> None:
    """Atomically increment global and daily usage counters."""
    now = _iso_now()
    keys = [USAGE_GLOBAL_KEY, _day_key()]
    values = {
        ":i": Decimal(max(0, input_tokens)),
        ":o": Decimal(max(0, output_tokens)),
        ":t": Decimal(max(0, total_tokens)),
        ":one": Decimal(1),
        ":br": Decimal(max(0, bedrock_rounds)),
        ":tc": Decimal(max(0, tool_calls)),
        ":now": now,
    }
    expr = (
        "ADD totalInputTokens :i, totalOutputTokens :o, totalTokens :t, "
        "requestCount :one, bedrockCallCount :br, toolCallCount :tc "
        "SET updatedAt = :now"
    )
    table = _table()
    for pk in keys:
        try:
            table.update_item(
                Key={"conversationId": pk},
                UpdateExpression=expr,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            log.warning("usage record failed key=%s: %s", pk, exc)


def get_usage_summary(*, history_days: int = 7) -> dict[str, Any]:
    """Return totals, per-day breakdown, budgets, and model id."""
    table = _table()
    try:
        global_resp = table.get_item(Key={"conversationId": USAGE_GLOBAL_KEY})
    except ClientError as exc:
        log.warning("usage load failed: %s", exc)
        raise RuntimeError("Failed to load usage metrics") from exc

    global_item = global_resp.get("Item") or {}
    totals = _counters(global_item)
    updated_at = global_item.get("updatedAt")

    daily: list[dict[str, Any]] = []
    for pk in _day_keys(history_days):
        try:
            resp = table.get_item(Key={"conversationId": pk})
        except ClientError:
            continue
        item = resp.get("Item")
        if not item:
            continue
        day = pk[len(USAGE_DAY_PREFIX) : -2]
        row = _counters(item)
        row["date"] = day
        daily.append(row)

    budget_total = _budget_total()
    budget_daily = _budget_daily()
    today_str = dt.datetime.now(dt.timezone.utc).date().isoformat()
    today_tokens = 0
    for row in daily:
        if row.get("date") == today_str:
            today_tokens = row["total_tokens"]
            break

    def pct(used: int, budget: int | None) -> float | None:
        if budget is None or budget <= 0:
            return None
        return round(min(100.0, (used / budget) * 100.0), 1)

    model_id = (os.environ.get("BEDROCK_MODEL_ID") or "").strip() or None

    return _json_safe(
        {
            "totals": totals,
            "updated_at": updated_at,
            "daily": daily,
            "budget": {
                "total_tokens": budget_total,
                "daily_tokens": budget_daily,
                "total_used_pct": pct(totals["total_tokens"], budget_total),
                "daily_used_pct": pct(today_tokens, budget_daily),
                "today_tokens": today_tokens,
            },
            "model_id": model_id,
            "max_tool_rounds": max(1, int(os.environ.get("AGENT_MAX_TOOL_ROUNDS", "5"))),
            "max_output_tokens": max(
                256, int(os.environ.get("AGENT_MAX_OUTPUT_TOKENS", "2048"))
            ),
        }
    )
