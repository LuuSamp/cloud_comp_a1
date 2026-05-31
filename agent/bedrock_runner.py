"""AWS Bedrock Converse loop with tool calling."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from agent import agent_functions

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the DijkFood operations assistant. Answer questions about order state, \
order history, routes, couriers, customers, and restaurants using only the provided tools.

Rules:
- Always use tools for factual data; never invent order IDs or statuses.
- You are read-only: do not claim to change orders or assign couriers.
- If a tool returns not_implemented, tool_disabled, or service_unavailable, explain clearly.
- Summarize tool results in clear natural language for operators.
"""


def _bedrock_region() -> str:
    return (
        (os.environ.get("BEDROCK_AWS_REGION") or "").strip()
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    )


def _model_id() -> str:
    mid = (os.environ.get("BEDROCK_MODEL_ID") or "").strip()
    if not mid:
        raise RuntimeError("BEDROCK_MODEL_ID is not configured")
    return mid


def _max_tool_rounds() -> int:
    return max(1, int(os.environ.get("AGENT_MAX_TOOL_ROUNDS", "5")))


def _max_tokens() -> int:
    return max(256, int(os.environ.get("AGENT_MAX_OUTPUT_TOKENS", "2048")))


def _bedrock_client() -> Any:
    key = (os.environ.get("BEDROCK_AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID") or "").strip()
    secret = (
        (os.environ.get("BEDROCK_AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY") or "").strip()
    )
    token = (
        (os.environ.get("BEDROCK_AWS_SESSION_TOKEN") or os.environ.get("AWS_SESSION_TOKEN") or "").strip()
        or None
    )
    region = _bedrock_region()
    if key and secret:
        session = boto3.Session(
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            aws_session_token=token,
            region_name=region,
        )
        return session.client("bedrock-runtime")
    return boto3.client("bedrock-runtime", region_name=region)


def _new_user_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"text": text}]}


def _extract_text_blocks(content: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in content:
        if "text" in block:
            parts.append(str(block["text"]))
    return "\n".join(parts).strip()


def _extract_tool_uses(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    uses: list[dict[str, Any]] = []
    for block in content:
        if "toolUse" in block:
            uses.append(block["toolUse"])
    return uses


def _usage_from_response(response: dict[str, Any]) -> dict[str, int]:
    raw = response.get("usage") or {}
    return {
        "input_tokens": int(raw.get("inputTokens") or 0),
        "output_tokens": int(raw.get("outputTokens") or 0),
        "total_tokens": int(raw.get("totalTokens") or 0),
    }


def _merge_usage(acc: dict[str, int], chunk: dict[str, int]) -> None:
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        acc[key] = acc.get(key, 0) + chunk.get(key, 0)


def run_chat(
    messages: list[dict[str, Any]],
    *,
    user_message: str,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """
    Append user_message, run Bedrock until end_turn or max rounds.
    Returns (reply_text, updated_messages, tools_used_summary, usage).
    """
    client = _bedrock_client()
    model_id = _model_id()
    tools = agent_functions.list_tools_for_bedrock()
    if not tools:
        raise RuntimeError("No agent tools enabled; set AGENT_ENABLED_TOOLS or register stable tools")

    working = list(messages)
    working.append(_new_user_message(user_message))
    tools_used: list[dict[str, Any]] = []
    usage: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "bedrock_rounds": 0,
    }

    tool_config: dict[str, Any] = {"tools": tools}
    inference_config = {
        "maxTokens": _max_tokens(),
        "temperature": float(os.environ.get("AGENT_TEMPERATURE", "0.2")),
    }

    for round_idx in range(_max_tool_rounds()):
        try:
            response = client.converse(
                modelId=model_id,
                system=[{"text": SYSTEM_PROMPT}],
                messages=working,
                toolConfig=tool_config,
                inferenceConfig=inference_config,
            )
        except (ClientError, BotoCoreError) as exc:
            log.exception("bedrock converse failed round=%s", round_idx)
            raise RuntimeError(f"Bedrock error: {exc}") from exc

        usage["bedrock_rounds"] += 1
        _merge_usage(usage, _usage_from_response(response))

        output_msg = response.get("output", {}).get("message")
        if not output_msg:
            raise RuntimeError("Bedrock returned no output message")

        working.append(output_msg)
        stop_reason = response.get("stopReason", "")

        if stop_reason == "tool_use":
            tool_uses = _extract_tool_uses(output_msg.get("content", []))
            if not tool_uses:
                break
            tool_result_blocks: list[dict[str, Any]] = []
            for tu in tool_uses:
                tool_use_id = tu.get("toolUseId", "")
                name = tu.get("name", "")
                raw_input = tu.get("input") or {}
                if isinstance(raw_input, str):
                    try:
                        raw_input = json.loads(raw_input)
                    except json.JSONDecodeError:
                        raw_input = {}
                result = agent_functions.run_tool(name, raw_input if isinstance(raw_input, dict) else {})
                tools_used.append({"name": name, "input": raw_input, "ok": result.get("ok", False)})
                tool_result_blocks.append(
                    {
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [{"json": result}],
                            "status": "success" if result.get("ok") else "error",
                        }
                    }
                )
            working.append({"role": "user", "content": tool_result_blocks})
            continue

        reply = _extract_text_blocks(output_msg.get("content", []))
        usage["tool_calls"] = len(tools_used)
        return reply or "(No response text from model.)", working, tools_used, usage

    reply = _extract_text_blocks(working[-1].get("content", [])) if working else ""
    usage["tool_calls"] = len(tools_used)
    return (
        reply or "I reached the maximum tool steps; please try a simpler question.",
        working,
        tools_used,
        usage,
    )
