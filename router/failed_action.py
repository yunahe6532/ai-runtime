"""Failed tool result compaction — planner constraints, not evidence artifacts."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from adapters.memory import SessionState, normalize_file_path

MAX_FAILED_SUMMARIES = int(__import__("os").getenv("MAX_FAILED_TOOL_SUMMARIES", "12"))
MAX_FAILED_PROMPT_ITEMS = int(__import__("os").getenv("MAX_FAILED_PROMPT_ITEMS", "5"))

_LARGE_FILE_RE = re.compile(
    r"exceeds maximum allowed characters|content exceeds maximum|file content \(\d+ characters\)",
    re.I,
)
_READ_GUARD_RE = re.compile(r"large_file|json_log_prefer_partial|full read blocked", re.I)


def failure_key(tool: str, path: str, reason: str) -> str:
    """Stable dedup key: tool + path + reason (never tool_call_id)."""
    return f"{tool}:{path or '?'}:{reason}"


def detect_tool_failure(
    tool_name: str,
    path: str,
    text: str,
    *,
    workspace: str = "",
) -> dict[str, Any] | None:
    """Return failed_action dict or None if this is a normal tool result."""
    t = (text or "").strip()
    if not t:
        return None
    tool = (tool_name or "tool").strip()
    norm_path = normalize_file_path(path, workspace) if path else ""

    if _LARGE_FILE_RE.search(t):
        return {
            "kind": "failed_action",
            "tool": tool,
            "path": norm_path or path,
            "reason": "large_file",
            "count": 1,
            "next_allowed": [
                "grep",
                "jq",
                "read_range",
                "docs/BENCHMARK.md",
            ],
        }

    if tool == "Read" and len(t) < 400 and (
        t.startswith("Error:") or "not found" in t.lower() or _READ_GUARD_RE.search(t)
    ):
        reason = "read_guard" if _READ_GUARD_RE.search(t) else "read_error"
        return {
            "kind": "failed_action",
            "tool": tool,
            "path": norm_path or path,
            "reason": reason,
            "count": 1,
            "next_allowed": ["grep", "read_range", "artifact_summary"],
        }

    if t.startswith("Error:") and tool in ("Read", "Grep", "Glob"):
        return {
            "kind": "failed_action",
            "tool": tool,
            "path": norm_path or path,
            "reason": "tool_error",
            "count": 1,
            "next_allowed": ["grep", "alternate path"],
        }

    return None


def _failure_key(item: dict[str, Any]) -> str:
    return failure_key(
        str(item.get("tool") or "?"),
        str(item.get("path") or "?"),
        str(item.get("reason") or "?"),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_failed_tool(state: SessionState, failure: dict[str, Any]) -> None:
    """Accumulate failed tool policy signals on session state (planner constraint, not evidence)."""
    summaries: list[dict[str, Any]] = list(getattr(state, "failed_tool_summaries", None) or [])
    key = _failure_key(failure)
    now = _utc_now()
    found = False
    for item in summaries:
        if _failure_key(item) == key:
            item["count"] = int(item.get("count") or 0) + 1
            item["last_seen_at"] = now
            found = True
            break
    if not found:
        entry = dict(failure)
        entry["last_seen_at"] = now
        summaries.append(entry)
    state.failed_tool_summaries = summaries[-MAX_FAILED_SUMMARIES:]

    state.failed_actions[key] = int(state.failed_actions.get(key, 0) or 0) + 1


def _planner_line(item: dict[str, Any]) -> str:
    path = item.get("path") or "?"
    reason = item.get("reason", "?")
    alts = item.get("next_allowed") or []
    alt = alts[0] if alts else "grep"
    if reason == "large_file":
        return f"- Do not full-read {path}; use {alt} or docs/BENCHMARK.md."
    return f"- {item.get('tool', '?')} {path} failed ({reason}); try {alt}."


def format_failed_tools_for_planner(state: SessionState, max_items: int | None = None) -> str:
    """Compact planner block — max 3–5 lines, no raw error dumps."""
    max_items = MAX_FAILED_PROMPT_ITEMS if max_items is None else max_items
    summaries = list(getattr(state, "failed_tool_summaries", None) or [])
    if not summaries:
        return ""
    lines = ["Recent blocked/failed actions:"]
    for item in summaries[-max_items:]:
        lines.append(_planner_line(item))
    return "\n".join(lines)


def format_failed_tools_block(state: SessionState, max_items: int | None = None) -> str:
    """Alias for planner-facing compact block."""
    return format_failed_tools_for_planner(state, max_items=max_items)
