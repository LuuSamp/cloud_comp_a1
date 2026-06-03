"""Agent meta-tools (no HTTP / database access)."""

from __future__ import annotations

from typing import Any

from agent.tools.registry import ToolSpec, object_schema, register_tool

UNRELATED_MESSAGE = "This is an unrelated question that I cannot answer"


def _report_unrelated_question(args: dict[str, Any]) -> dict[str, Any]:
    reason = (args.get("reason") or "").strip()
    result: dict[str, Any] = {
        "ok": True,
        "unrelated": True,
        "message": UNRELATED_MESSAGE,
    }
    if reason:
        result["reason"] = reason
    return result


def register_guardrail_tools() -> None:
    register_tool(
        ToolSpec(
            name="report_unrelated_question",
            description=(
                "Call ONLY as a last resort after you have tried every relevant DijkFood tool "
                "and none can answer the question (missing data, out of scope, or not about "
                "orders, couriers, customers, restaurants, routes, or operation history). "
                "Do NOT call for general knowledge, jokes, or topics outside DijkFood operations. "
                "Returns a fixed refusal message to relay to the user."
            ),
            input_schema=object_schema(
                {
                    "reason": {
                        "type": "string",
                        "description": (
                            "Brief note on why the question is unrelated or could not be "
                            "answered with available tools (for logs only)."
                        ),
                    },
                }
            ),
            handler=_report_unrelated_question,
            service="agent",
            status="stable",
            endpoint_ref="(local guardrail)",
            always_enabled=True,
        )
    )
