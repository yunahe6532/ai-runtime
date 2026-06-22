"""Large-file Read guard — redirect to partial reads instead of blocking."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

LARGE_READ_LIMIT = int(os.getenv("LARGE_READ_LIMIT", "50000"))
CURSOR_READ_MAX = int(os.getenv("CURSOR_READ_MAX", "100000"))

_LARGE_JSON_PATTERNS = (
    re.compile(r"benchmark-runtime-score\.json$", re.I),
    re.compile(r"benchmark-cursor-agent\.json$", re.I),
    re.compile(r"agent-runs/.*\.json$", re.I),
    re.compile(r"\.flow\.json$", re.I),
    re.compile(r"tmp/benchmark-.*\.json$", re.I),
)

_KNOWN_SUMMARY_PATHS = {
    "docs/BENCHMARK.md": "benchmark summary (prefer over raw JSON)",
    "docs/ARCHITECTURE.md": "architecture overview",
    "handoff.md": "recent handoff notes",
}


def is_large_json_log_path(path: str) -> bool:
    norm = path.replace("\\", "/")
    return any(p.search(norm) for p in _LARGE_JSON_PATTERNS)


def get_file_size(path: str) -> int | None:
    if not path:
        return None
    try:
        p = Path(path)
        if p.is_file():
            return p.stat().st_size
    except OSError:
        pass
    return None


def _jq_hint(path: str) -> str:
    base = Path(path).name
    if "runtime-score" in base:
        return (
            f"jq '.runs[-1].runtime_score' {path} "
            f"OR jq '.runs[-1].summary' {path}"
        )
    if "cursor-agent" in base:
        return f"jq '.runs[-1].summary' {path}"
    if base.endswith(".flow.json"):
        return (
            f"jq '.stages[] | select(.stage==\"2_router_proxy\")' {path} "
            f"OR jq '.stages[-1]' {path}"
        )
    if "agent-runs" in path:
        return f"jq '{{status, intent, phase, turn_index, error}}' {path}"
    return f"jq 'keys' {path}"


def build_read_alternatives(path: str, size: int | None = None) -> dict[str, Any]:
    """Return structured redirect when full Read is inappropriate."""
    size_note = f" ({size:,} bytes)" if size else ""
    alts: list[str] = []
    if is_large_json_log_path(path):
        alts.extend([
            f"Grep: search field in {Path(path).name}",
            f"Shell: {_jq_hint(path)}",
            f"Read with offset/limit (first 80 lines)",
            "Read docs/BENCHMARK.md for curated summary",
        ])
    else:
        alts.extend([
            f"Grep: search pattern in {Path(path).name}",
            f"Read with offset=1 limit=80",
            f"Shell: head -n 40 {path}",
            f"Shell: tail -n 40 {path}",
        ])
    for rel, hint in _KNOWN_SUMMARY_PATHS.items():
        if rel not in path:
            alts.append(f"Read {rel} — {hint}")
    return {
        "blocked": True,
        "reason": "large_file",
        "path": path,
        "size": size,
        "message": (
            f"Full Read blocked for large file{size_note}. "
            "Use partial access instead of loading entire file."
        ),
        "next_allowed": alts[:5],
    }


def check_read_allowed(
    path: str,
    args: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Return (allowed, reason, redirect_info).

    Range/offset reads are always allowed even on large files.
    """
    args = args or {}
    if not path:
        return True, "", None

    offset = args.get("offset")
    limit = args.get("limit")
    if offset is not None or limit is not None:
        return True, "", None

    size = get_file_size(path)
    large = size is not None and size > LARGE_READ_LIMIT
    cursor_oversize = size is not None and size > CURSOR_READ_MAX
    json_log = is_large_json_log_path(path)

    if json_log and not large:
        # JSON logs: prefer partial even below LARGE_READ_LIMIT
        info = build_read_alternatives(path, size)
        return False, "json_log_prefer_partial", info

    if large or cursor_oversize:
        info = build_read_alternatives(path, size)
        return False, "large_file", info

    return True, "", None


def format_read_guard_message(info: dict[str, Any]) -> str:
    lines = [info.get("message", "Read redirected to partial access.")]
    for alt in info.get("next_allowed") or []:
        lines.append(f"- {alt}")
    return "\n".join(lines)


def grep_instead_of_read(path: str, field: str = "") -> dict[str, Any]:
    """Suggest Grep as primary alternative for JSON/log paths."""
    pattern = field or "pass_rate|runtime_score|status|phase|intent"
    return {
        "tool": "Grep",
        "args": {"pattern": pattern, "path": path},
        "reason": "extract specific fields without full file read",
    }
