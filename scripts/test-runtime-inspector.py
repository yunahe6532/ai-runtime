#!/usr/bin/env python3
"""Unit tests for runtime_inspector (Cursor Runtime Inspector content mirror)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from reference.agent_exec import completion_json_to_sse
from adapters.memory import SessionState
from runtime_inspector import (
    RuntimeInspectorContext,
    build_inspector_from_state,
    build_runtime_inspector,
    inject_runtime_inspector,
)


def test_snapshot_contains_evidence_progress():
    ctx = RuntimeInspectorContext(
        run_id="1781761482_0006",
        phase="tool_planning",
        query="프로젝트 구조 파악",
        agent_plan={
            "task_intent": "project_inspection",
            "confidence": 0.91,
            "goal": "프로젝트 구조 파악",
            "evidence_needed": ["project_tree_seen", "core_files_seen"],
            "evidence_collected": ["project_tree_seen"],
            "next_action": {"tool": "Read", "target": "router/planner.py", "reason": "core file"},
            "avoid_actions": ["Glob unless Read fails"],
        },
        raw_tokens=21430,
        pack_tokens=2143,
        saved_pct=91.0,
        cursor_message_count=142,
        proxy_message_count=12,
        llm_elapsed_sec=2.31,
    )
    md = build_runtime_inspector(ctx)
    assert "<details>" in md
    assert "Runtime Snapshot" in md
    assert "✔ Project Tree" in md
    assert "□ Core Files" in md
    assert "Timeline" in md
    assert "Telemetry" in md
    assert "Context" in md
    assert "Memory" in md
    assert "Dashboard" in md
    assert "0.91" in md
    assert "91%" in md or "91.0%" in md
    print("PASS test_snapshot_contains_evidence_progress")


def test_sse_skips_inspector_during_tool_planning():
    resp = {
        "id": "test",
        "created": 1,
        "model": "model.gguf",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "Read", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    inject_runtime_inspector(resp, "<details><summary>Runtime Inspector</summary>\n\nTest\n\n</details>")
    sse = completion_json_to_sse(resp, phase="tool_planning").decode()
    chunks = []
    for line in sse.strip().split("\n\n"):
        if line.startswith("data:") and "[DONE]" not in line:
            chunks.append(json.loads(line[6:])["choices"][0].get("delta", {}))
    assert chunks[0] == {"role": "assistant"}
    assert not any("Runtime Inspector" in str(c.get("content", "")) for c in chunks)
    assert not any(c.get("reasoning_content") for c in chunks)
    assert any("tool_calls" in c for c in chunks)
    print("PASS test_sse_skips_inspector_during_tool_planning")


def test_sse_final_answer_prose_only_no_inspector():
    resp = {
        "id": "test",
        "created": 1,
        "model": "model.gguf",
        "choices": [
            {
                "message": {"role": "assistant", "content": "최종 구조 답변입니다."},
                "finish_reason": "stop",
            }
        ],
    }
    inject_runtime_inspector(resp, "<details><summary>Runtime Inspector</summary>\n\nSnap\n\n</details>")
    sse = completion_json_to_sse(resp, phase="final_answer").decode()
    deltas = []
    for line in sse.strip().split("\n\n"):
        if line.startswith("data:") and "[DONE]" not in line:
            d = json.loads(line[6:])["choices"][0].get("delta", {})
            if d:
                deltas.append(d)
    content_deltas = [str(d.get("content", "")) for d in deltas if d.get("content")]
    assert any("최종 구조" in c for c in content_deltas)
    assert not any("Runtime Inspector" in c for c in content_deltas)
    print("PASS test_sse_final_answer_prose_only_no_inspector")


def test_sse_emits_guard_promote_prose_in_tool_planning():
    resp = {
        "id": "test",
        "created": 1,
        "model": "model.gguf",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "수집된 source registry 기준으로 router 디렉터리 역할을 정리합니다.",
                    "_runtime_inspector": "<details><summary>Runtime</summary>\n\nX\n\n</details>",
                },
                "finish_reason": "stop",
            }
        ],
    }
    sse = completion_json_to_sse(resp, phase="tool_planning").decode()
    assert "수집된 source registry" in sse
    assert "Runtime</summary>" not in sse
    print("PASS test_sse_emits_guard_promote_prose_in_tool_planning")


def test_build_from_session_state():
    state = SessionState(
        current_query="bench",
        files_read=["/home/yunahe/ai-runtime/cursor-local-llm/router/planner.py"],
        commands_run=["ls -la"],
        turn_index=6,
        agent_plan={
            "task_intent": "project_inspection",
            "evidence_needed": ["project_tree_seen"],
            "evidence_collected": ["project_tree_seen"],
            "step_count": 3,
        },
    )

    class Stats:
        raw_tokens = 10000
        pack_tokens = 900
        saved_pct = 91.0
        backend = "long"
        intent = "agent"

    md = build_inspector_from_state(
        state,
        run_id="run_1",
        phase="tool_planning",
        stats=Stats(),
        cursor_message_count=50,
        proxy_message_count=8,
        llm_elapsed_sec=1.2,
    )
    assert "run_1" in md
    assert "planner.py" in md or "Planner" in md
    print("PASS test_build_from_session_state")


def main() -> int:
    test_snapshot_contains_evidence_progress()
    test_sse_skips_inspector_during_tool_planning()
    test_sse_final_answer_prose_only_no_inspector()
    test_sse_emits_guard_promote_prose_in_tool_planning()
    test_build_from_session_state()
    print("\nAll runtime inspector tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
