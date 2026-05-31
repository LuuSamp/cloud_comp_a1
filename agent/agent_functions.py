"""
Public facade for the conversational agent tool layer.

Bedrock and the API must call only run_tool / list_tools_for_bedrock here—not microservices or the database directly.
"""

from __future__ import annotations

from typing import Any

import agent.tools  # noqa: F401 — registers tools

from agent.tools.registry import list_tools_for_bedrock as list_tools_for_bedrock
from agent.tools.registry import list_tools_metadata as list_tools_metadata
from agent.tools.registry import run_tool as run_tool

__all__ = ["run_tool", "list_tools_for_bedrock", "list_tools_metadata"]
