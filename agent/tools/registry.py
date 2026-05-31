"""Tool registry for the conversational agent."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

ToolStatus = Literal["stable", "beta", "stub"]
ToolService = Literal["ordering", "tracking", "routing"]

_REGISTRY: dict[str, ToolSpec] = {}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    service: ToolService
    status: ToolStatus = "stable"
    endpoint_ref: str = ""


def register_tool(spec: ToolSpec) -> None:
    if spec.name in _REGISTRY:
        raise ValueError(f"duplicate tool name: {spec.name}")
    _REGISTRY[spec.name] = spec


def get_tool(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def _parse_name_list(env_key: str) -> set[str] | None:
    raw = (os.environ.get(env_key) or "").strip()
    if not raw:
        return None
    return {p.strip() for p in raw.split(",") if p.strip()}


def _is_tool_enabled(spec: ToolSpec) -> bool:
    enabled = _parse_name_list("AGENT_ENABLED_TOOLS")
    disabled = _parse_name_list("AGENT_DISABLED_TOOLS") or set()
    if spec.name in disabled:
        return False
    if enabled is not None:
        return spec.name in enabled
    if spec.status == "stub":
        return False
    return spec.status in ("stable", "beta")


def list_enabled_tools() -> list[ToolSpec]:
    return [s for s in _REGISTRY.values() if _is_tool_enabled(s)]


def list_tools_for_bedrock() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for spec in sorted(list_enabled_tools(), key=lambda s: s.name):
        tools.append(
            {
                "toolSpec": {
                    "name": spec.name,
                    "description": spec.description,
                    "inputSchema": {"json": spec.input_schema},
                }
            }
        )
    return tools


def list_tools_metadata() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in sorted(_REGISTRY.values(), key=lambda s: s.name):
        out.append(
            {
                "name": spec.name,
                "service": spec.service,
                "status": spec.status,
                "enabled": _is_tool_enabled(spec),
                "endpoint_ref": spec.endpoint_ref,
            }
        )
    return out


def _stub_result(tool: str) -> dict[str, Any]:
    return {"ok": False, "error": "not_implemented", "tool": tool}


def run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    spec = get_tool(name)
    if spec is None:
        return {"ok": False, "error": "unknown_tool", "tool": name}
    if not _is_tool_enabled(spec):
        return {"ok": False, "error": "tool_disabled", "tool": name}
    if spec.status == "stub":
        return _stub_result(name)
    try:
        return spec.handler(args)
    except Exception as exc:
        return {"ok": False, "error": "handler_error", "tool": name, "detail": str(exc)}


def object_schema(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
