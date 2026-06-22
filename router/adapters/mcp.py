"""MCP tool protocol adapter (v2 Agent Runtime scope) — stub."""

from __future__ import annotations

import os
from typing import Any

MCP_ENABLED = os.getenv("MCP_ENABLED", "0") == "1"


def mcp_enabled() -> bool:
    return MCP_ENABLED


def normalize_tool_result(result: Any) -> dict[str, Any]:
    """Placeholder — tool execution stays in Application (Cursor) for v1."""
    if isinstance(result, dict):
        return result
    return {"content": str(result)}
