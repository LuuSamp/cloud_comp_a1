"""Hybrid usage metrics: CloudWatch for Bedrock tokens, DynamoDB for app counters."""

from __future__ import annotations

import datetime as dt
import logging
import os
from decimal import Decimal
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from agent.aws_clients import bedrock_aws_region, cloudwatch_client
from agent.sessions import _iso_now, _json_safe, _table

log = logging.getLogger(__name__)

USAGE_GLOBAL_KEY = "__usage__global__"
USAGE_DAY_PREFIX = "__usage__day__"
CW_NAMESPACE = "AWS/Bedrock"
CW_PERIOD_SECONDS = 86400


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


def _history_days() -> int:
    raw = (os.environ.get("AGENT_USAGE_HISTORY_DAYS") or "7").strip()
    try:
        return max(1, min(90, int(raw)))
    except ValueError:
        return 7


def _day_key(day: dt.date | None = None) -> str:
    d = day or dt.datetime.now(dt.timezone.utc).date()
    return f"{USAGE_DAY_PREFIX}{d.isoformat()}__"


def _day_keys(days: int) -> list[str]:
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


def _app_counters(item: dict[str, Any] | None) -> dict[str, int]:
    return {
        "request_count": _to_int(item, "requestCount"),
        "tool_calls": _to_int(item, "toolCallCount"),
    }


def _model_id() -> str:
    mid = (os.environ.get("BEDROCK_MODEL_ID") or "").strip()
    if not mid:
        raise RuntimeError("BEDROCK_MODEL_ID is not configured")
    return mid


def _model_id_candidates(model_id: str, region: str) -> list[str]:
    candidates = [model_id]
    arn = f"arn:aws:bedrock:{region}::foundation-model/{model_id}"
    if arn not in candidates:
        candidates.append(arn)
    return candidates


def _metric_query(metric_name: str, query_id: str, model_id_value: str) -> dict[str, Any]:
    return {
        "Id": query_id,
        "MetricStat": {
            "Metric": {
                "Namespace": CW_NAMESPACE,
                "MetricName": metric_name,
                "Dimensions": [{"Name": "ModelId", "Value": model_id_value}],
            },
            "Period": CW_PERIOD_SECONDS,
            "Stat": "Sum",
        },
        "ReturnData": True,
    }


def _cloudwatch_access_error(exc: ClientError) -> RuntimeError:
    code = exc.response.get("Error", {}).get("Code", "")
    if code in ("AccessDenied", "AccessDeniedException"):
        return RuntimeError(
            "CloudWatch access denied for Bedrock metrics; "
            "add cloudwatch:GetMetricData to the Bedrock account IAM user"
        )
    return RuntimeError(f"Failed to load CloudWatch usage metrics: {exc}")


def _fetch_cloudwatch_usage(model_id: str, history_days: int) -> dict[str, Any]:
    """Return daily token/invocation rows and totals from CloudWatch."""
    region = bedrock_aws_region()
    client = cloudwatch_client()
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=history_days)

    best_resp: dict[str, Any] | None = None
    for candidate in _model_id_candidates(model_id, region):
        queries = [
            _metric_query("InputTokenCount", "input", candidate),
            _metric_query("OutputTokenCount", "output", candidate),
            _metric_query("Invocations", "invocations", candidate),
        ]
        try:
            resp = client.get_metric_data(
                MetricDataQueries=queries,
                StartTime=start,
                EndTime=end,
            )
        except ClientError as exc:
            raise _cloudwatch_access_error(exc) from exc
        except BotoCoreError as exc:
            raise RuntimeError(f"Failed to load CloudWatch usage metrics: {exc}") from exc

        has_data = any(r.get("Values") for r in resp.get("MetricDataResults", []))
        best_resp = resp
        if has_data:
            break

    assert best_resp is not None
    results_by_id = {r["Id"]: r for r in best_resp.get("MetricDataResults", [])}

    daily_map: dict[str, dict[str, int]] = {}

    def merge_series(query_id: str, field: str) -> None:
        result = results_by_id.get(query_id, {})
        for ts, val in zip(result.get("Timestamps", []), result.get("Values", [])):
            day = ts.astimezone(dt.timezone.utc).date().isoformat()
            row = daily_map.setdefault(
                day,
                {"input_tokens": 0, "output_tokens": 0, "bedrock_rounds": 0},
            )
            row[field] += int(val)

    merge_series("input", "input_tokens")
    merge_series("output", "output_tokens")
    merge_series("invocations", "bedrock_rounds")

    today = end.date()
    daily: list[dict[str, Any]] = []
    for i in range(history_days):
        day = (today - dt.timedelta(days=i)).isoformat()
        row_data = daily_map.get(
            day,
            {"input_tokens": 0, "output_tokens": 0, "bedrock_rounds": 0},
        )
        daily.append(
            {
                "date": day,
                "input_tokens": row_data["input_tokens"],
                "output_tokens": row_data["output_tokens"],
                "total_tokens": row_data["input_tokens"] + row_data["output_tokens"],
                "bedrock_rounds": row_data["bedrock_rounds"],
            }
        )

    totals = {
        "input_tokens": sum(r["input_tokens"] for r in daily),
        "output_tokens": sum(r["output_tokens"] for r in daily),
        "total_tokens": sum(r["total_tokens"] for r in daily),
        "bedrock_rounds": sum(r["bedrock_rounds"] for r in daily),
    }
    return {"daily": daily, "totals": totals}


def _load_dynamo_app_usage(history_days: int) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """Load request_count and tool_calls from DynamoDB (global + per-day)."""
    table = _table()
    try:
        global_resp = table.get_item(Key={"conversationId": USAGE_GLOBAL_KEY})
    except ClientError as exc:
        log.warning("usage load failed: %s", exc)
        raise RuntimeError("Failed to load usage metrics") from exc

    global_app = _app_counters(global_resp.get("Item"))
    daily_app: dict[str, dict[str, int]] = {}
    for pk in _day_keys(history_days):
        try:
            resp = table.get_item(Key={"conversationId": pk})
        except ClientError:
            continue
        item = resp.get("Item")
        if not item:
            continue
        day = pk[len(USAGE_DAY_PREFIX) : -2]
        daily_app[day] = _app_counters(item)
    return global_app, daily_app


def record_chat_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    bedrock_rounds: int,
    tool_calls: int,
) -> None:
    """Increment chat request and tool-call counters in DynamoDB (tokens from CloudWatch)."""
    del input_tokens, output_tokens, total_tokens, bedrock_rounds
    now = _iso_now()
    keys = [USAGE_GLOBAL_KEY, _day_key()]
    values = {
        ":one": Decimal(1),
        ":tc": Decimal(max(0, tool_calls)),
        ":now": now,
    }
    expr = "ADD requestCount :one, toolCallCount :tc SET updatedAt = :now"
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


def get_usage_summary(*, history_days: int | None = None) -> dict[str, Any]:
    """Return merged CloudWatch token metrics and DynamoDB app counters."""
    days = history_days if history_days is not None else _history_days()
    model_id = _model_id()

    cw = _fetch_cloudwatch_usage(model_id, days)
    global_app, daily_app = _load_dynamo_app_usage(days)

    daily: list[dict[str, Any]] = []
    for row in cw["daily"]:
        app = daily_app.get(row["date"], {"request_count": 0, "tool_calls": 0})
        daily.append({**row, **app})

    totals = {
        **cw["totals"],
        "request_count": global_app["request_count"],
        "tool_calls": global_app["tool_calls"],
    }

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

    return _json_safe(
        {
            "totals": totals,
            "updated_at": _iso_now(),
            "daily": daily,
            "budget": {
                "total_tokens": budget_total,
                "daily_tokens": budget_daily,
                "total_used_pct": pct(totals["total_tokens"], budget_total),
                "daily_used_pct": pct(today_tokens, budget_daily),
                "today_tokens": today_tokens,
            },
            "model_id": model_id,
            "usage_history_days": days,
            "max_tool_rounds": max(1, int(os.environ.get("AGENT_MAX_TOOL_ROUNDS", "5"))),
            "max_output_tokens": max(
                256, int(os.environ.get("AGENT_MAX_OUTPUT_TOKENS", "2048"))
            ),
        }
    )
