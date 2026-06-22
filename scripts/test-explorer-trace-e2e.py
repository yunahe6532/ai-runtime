#!/usr/bin/env python3
"""Phase 2.05 — Explorer / planner trace E2E tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from agent_brain.planner_shadow import run_planner_shadow  # noqa: E402
from explorer_trace import (  # noqa: E402
    diagnose_trace_file,
    format_flow_event,
    replay_trace_file,
    write_explorer_trace,
)
from legacy.memory_store import SessionState  # noqa: E402


def test_trace_file_creation(tmp: Path) -> None:
    trace = tmp / "explorer-trace.ndjson"
    os.environ["EXPLORER_TRACE_PATH"] = str(trace)
    write_explorer_trace(
        "tool.requested",
        phase="tool_planning",
        query="test query",
        turn_index=1,
        tool_name="Read",
        tool_args={"path": "router/main.py"},
        result_summary="read requested",
    )
    assert trace.is_file()
    lines = trace.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "tool.requested"
    assert row["req_id"] is not None or row.get("flow_id") is not None
    assert row["phase"] == "tool_planning"
    assert row["tool_name"] == "Read"
    print("PASS test_trace_file_creation")


def test_planner_shadow_events(tmp: Path) -> None:
    trace = tmp / "shadow.ndjson"
    os.environ["EXPLORER_TRACE_PATH"] = str(trace)
    state = SessionState()
    state.current_query = "analyze architecture"
    state.turn_index = 3
    state.agent_plan = {
        "next_action": {"tool": "Read", "target": "router/main.py", "reason": "entry"},
        "evidence_needed": ["core_files_seen"],
        "evidence_collected": [],
        "confidence": 0.8,
    }
    state.project_index = {"file_count": 10, "entrypoints": ["router/main.py"]}
    state.last_working_set = {"priority_targets": ["router/main.py"], "must_include": []}
    run_planner_shadow(
        state,
        query=state.current_query,
        phase="tool_planning",
        router_intent="read_only_analysis",
        coverage=SimpleNamespace(to_dict=lambda: {"complete": False, "coverage_score": 0.3}),
    )
    events = [json.loads(ln) for ln in trace.read_text(encoding="utf-8").splitlines() if ln.strip()]
    kinds = {e["event"] for e in events}
    assert "planner.runtime_state.created" in kinds
    assert "planner.shadow.proposed" in kinds
    assert "planner.shadow.compared" in kinds
    compared = [e for e in events if e["event"] == "planner.shadow.compared"][0]
    assert "would_change_hot_path" in compared
    assert state.last_planner_shadow.get("would_change_hot_path") is not None
    print("PASS test_planner_shadow_events")


def test_malformed_line_ignored(tmp: Path) -> None:
    trace = tmp / "malformed.ndjson"
    trace.write_text(
        '{"event":"plan","ts":"2026-01-01T00:00:00+00:00","step":1}\n'
        "NOT JSON\n"
        '{"event":"planner.shadow.compared","ts":"2026-01-01T00:00:01+00:00",'
        '"decision":"read","match":true,"reason":"ok"}\n',
        encoding="utf-8",
    )
    blocks = replay_trace_file(trace, from_start=True)
    assert len(blocks) >= 1
    diag = diagnose_trace_file(trace)
    assert diag["line_count"] >= 2
    assert diag["malformed_lines"] >= 1
    print("PASS test_malformed_line_ignored")


def test_missing_file_diagnosis(tmp: Path) -> None:
    missing = tmp / "nope.ndjson"
    diag = diagnose_trace_file(missing)
    assert diag["status"] == "missing"
    assert "missing" in diag.get("message", "").lower()
    print("PASS test_missing_file_diagnosis")


def test_tail_explorer_from_start_exits(tmp: Path) -> None:
    trace = tmp / "flow.ndjson"
    write_explorer_trace = __import__("explorer_trace").write_explorer_trace
    os.environ["EXPLORER_TRACE_PATH"] = str(trace)
    write_explorer_trace(
        "planner.shadow.compared",
        phase="tool_planning",
        query="q",
        decision="read",
        match=True,
        reason="aligned",
        result_summary="rule=read shadow=read",
    )
    write_explorer_trace(
        "memory.journal.appended",
        target="router/x.py",
        tool_name="read",
        result_summary="read x",
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/tail-explorer-flow.py"),
            str(trace),
            "--from-start",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "planner.shadow.compared" in proc.stdout or "read" in proc.stdout
    assert "memory.journal.appended" in proc.stdout or "router/x.py" in proc.stdout
    print("PASS test_tail_explorer_from_start_exits")


def test_format_new_event_types() -> None:
    row = {
        "ts": "2026-06-22T00:00:00+00:00",
        "event": "planner.shadow.compared",
        "decision": "grep",
        "match": False,
        "mismatch_reason": "action_mismatch",
        "would_change_hot_path": True,
        "reason": "shadow differs",
    }
    out = format_flow_event(row) or ""
    assert "planner.shadow.compared" in out
    assert "would_change_hot_path" in out
    print("PASS test_format_new_event_types")


def main() -> int:
    os.environ["EXPLORER_TRACE_ENABLED"] = "1"
    os.environ["EXPLORER_TRACE_STDOUT"] = "0"
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_trace_file_creation(tmp)
        test_planner_shadow_events(tmp)
        test_malformed_line_ignored(tmp)
        test_missing_file_diagnosis(tmp)
        test_tail_explorer_from_start_exits(tmp)
    test_format_new_event_types()
    print("\nAll explorer trace E2E tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
