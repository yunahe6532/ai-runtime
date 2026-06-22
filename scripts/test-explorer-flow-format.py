#!/usr/bin/env python3
"""Unit tests for explorer flow transcript formatting."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from explorer_trace import format_flow_event  # noqa: E402


def test_plan_shows_thinking_and_next_tool() -> None:
    row = {
        "ts": "2026-06-21T17:21:41.920569+00:00",
        "event": "plan",
        "flow_id": "1782062499_0001",
        "step": 0,
        "decision_source": "llm",
        "thinking": "Need to inventory runtime_core first.",
        "next_tool": "GlobSource",
        "next_sid": "dir.runtime_core",
        "next_glob": "*.py",
        "checklist_pending": ["glob:dir.adapters", "grep_content:dir.adapters"],
    }
    out = format_flow_event(row) or ""
    assert "plan · step 0" in out
    assert "thinking" in out
    assert "Need to inventory runtime_core" in out
    assert "→ Glob dir.runtime_core *.py" in out
    assert "glob:dir.adapters" in out


def test_action_done_shows_result_preview() -> None:
    row = {
        "ts": "2026-06-21T17:21:47.328470+00:00",
        "event": "action_done",
        "flow_id": "1782062503_0002",
        "tool": "Glob",
        "source_id": "dir.adapters",
        "success": True,
        "result_chars": 197,
        "action_sig": "glob:dir.adapters:*.py",
        "result_preview": "adapters/gateway.py\nadapters/observe.py",
    }
    out = format_flow_event(row) or ""
    assert "done · Glob dir.adapters" in out
    assert "adapters/gateway.py" in out
    assert "glob:dir.adapters:*.py" in out


def test_alternating_sequence_readable() -> None:
    rows = [
        {
            "ts": "2026-06-21T17:21:41+00:00",
            "event": "plan",
            "step": 0,
            "decision_source": "llm",
            "thinking": "Start with glob.",
            "next_tool": "GlobSource",
            "next_sid": "dir.runtime_core",
            "next_glob": "*.py",
        },
        {
            "ts": "2026-06-21T17:21:42+00:00",
            "event": "action_emit",
            "tool": "GlobSource",
            "source_id": "dir.runtime_core",
            "glob_pattern": "*.py",
        },
        {
            "ts": "2026-06-21T17:21:43+00:00",
            "event": "action_done",
            "tool": "Glob",
            "source_id": "dir.runtime_core",
            "success": True,
            "result_chars": 50,
            "result_preview": "runtime_core/budget.py",
        },
        {
            "ts": "2026-06-21T17:21:44+00:00",
            "event": "plan",
            "step": 1,
            "decision_source": "llm",
            "thinking": "Now grep imports.",
            "next_tool": "GrepSource",
            "next_sid": "dir.adapters",
            "next_pattern": "import |from",
        },
    ]
    transcript = "\n\n".join(format_flow_event(r) or "" for r in rows)
    assert transcript.index("thinking") < transcript.index("done · Glob")
    assert transcript.index("done · Glob") < transcript.index("Now grep imports")


def main() -> int:
    test_plan_shows_thinking_and_next_tool()
    test_action_done_shows_result_preview()
    test_alternating_sequence_readable()
    print("OK: explorer flow format tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
