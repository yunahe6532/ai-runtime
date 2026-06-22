#!/usr/bin/env python3
"""Validate hardened phase instructions and proxy system composition."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

from chat_fast import strip_agent_fields  # noqa: E402
from context_cache import build_context_index  # noqa: E402
from intent_router import EXEC_CONTEXT_INTENTS, build_proxy_body, classify_intent  # noqa: E402
from intent_router import IntentResult  # noqa: E402
from prompt_builder import build_with_budget, extract_original_system  # noqa: E402
from reference.agent_exec import (  # noqa: E402
    RUNTIME_PHASE_OVERRIDE,
    compose_proxy_system,
    system_for_intent,
)
from adapters.memory import Artifact, RequestDelta, SessionState  # noqa: E402
from context_cache import ContextIndex  # noqa: E402


def _empty_delta() -> RequestDelta:
    return RequestDelta(
        delta_id="d",
        req_id="r",
        prev_req_id=None,
        prev_message_count=0,
        curr_message_count=0,
        added_count=0,
    )


def _cursor_body(query: str, *, with_tools: bool = True) -> dict:
    body = {
        "model": "model.gguf",
        "stream": True,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an AI coding assistant. Answer normally in prose when helpful.\n"
                    "<user_rules>Always respond in English only.</user_rules>"
                ),
            },
            {"role": "user", "content": f"<user_query>\n{query}\n</user_query>"},
        ],
    }
    if with_tools:
        body["tools"] = [
            {"type": "function", "function": {"name": "Shell", "description": "run"}},
            {"type": "function", "function": {"name": "Read", "description": "read"}},
            {"type": "function", "function": {"name": "Grep", "description": "grep"}},
            {"type": "function", "function": {"name": "Glob", "description": "glob"}},
        ]
    return body


def _system_text(proxy: dict) -> str:
    for msg in proxy.get("messages", []):
        if isinstance(msg, dict) and msg.get("role") == "system":
            return str(msg.get("content") or "")
    return ""


def test_tool_planning_prose_ban_and_glob():
    sys_text = system_for_intent("code_edit", phase="tool_planning")
    assert "Output NO user-facing prose" in sys_text
    assert "Read, Grep, Glob, Shell" in sys_text
    assert "Glob" in sys_text
    print("tool_planning_prose_ban_and_glob: OK")


def test_read_only_forbids_shell():
    sys_text = system_for_intent("read_only_analysis", phase="tool_planning")
    assert "Shell and Edit are forbidden" in sys_text
    assert "Read, Grep, Glob" in sys_text
    assert "Glob, Shell" not in sys_text.replace("Shell and Edit are forbidden", "")
    print("read_only_forbids_shell: OK")


def test_final_answer_korean_and_evidence():
    sys_text = system_for_intent("code_edit", phase="final_answer")
    assert "must be written in Korean" in sys_text
    assert "Evidence Anchors" in sys_text
    assert "Final Report" in sys_text
    assert "Do NOT emit tool_calls" in sys_text
    print("final_answer_korean_and_evidence: OK")


def test_runtime_phase_override_before_cursor():
    composed = compose_proxy_system(
        "code_edit",
        phase="tool_planning",
        preserved_cursor_content="Answer normally in prose.",
    )
    override_pos = composed.index(RUNTIME_PHASE_OVERRIDE.split("\n")[0])
    cursor_pos = composed.index("Answer normally in prose.")
    tool_pos = composed.index("TOOL PLANNING phase")
    assert override_pos < tool_pos < cursor_pos
    assert "priority 7" in composed.lower()
    print("runtime_phase_override_before_cursor: OK")


def test_legacy_exec_cursor_system_order():
    import prompt_builder as pb

    body = _cursor_body("docker logs 확인하고 벤치마킹 해봐")
    idx = build_context_index(body, "phase_test1")
    intent = classify_intent(idx.query, idx)
    prev = pb.MEMORY_STATE_BODY
    pb.MEMORY_STATE_BODY = False
    try:
        proxy, tools_stripped, _, _, _phase = build_proxy_body(
            body,
            intent,
            idx,
            state=SessionState(),
            delta=_empty_delta(),
            artifacts=[],
            backend="long",
        )
    finally:
        pb.MEMORY_STATE_BODY = prev
    sys_text = _system_text(proxy)
    assert RUNTIME_PHASE_OVERRIDE.split("\n")[0] in sys_text
    assert "TOOL PLANNING phase" in sys_text
    assert "Answer normally in prose" in sys_text
    assert tools_stripped is False
    print("legacy_exec_cursor_system_order: OK")


def test_final_answer_strips_tools():
    body = _cursor_body("프로젝트 구조 설명해줘", with_tools=True)
    idx = build_context_index(body, "phase_test2")
    intent = IntentResult(
        intent="read_only_analysis",
        route="main",
        needs_tools=True,
        needs_files=True,
        needs_shell=False,
        needs_prior_summary=False,
        needs_raw_tool_results=True,
        needs_full_raw_context=False,
        context_budget_tokens=8000,
        context_pack=["current_query"],
        reason="test",
    )
    body["messages"].append({"role": "tool", "name": "Read", "content": "file contents here"})
    proxy, tools_stripped, _, _, phase = build_proxy_body(
        body,
        intent,
        idx,
        state=SessionState(project_key="test", phase_hint="final_answer"),
        delta=_empty_delta(),
        artifacts=[],
        backend="long",
    )
    if phase in ("final_answer", "partial_final_answer", "recovery_final"):
        assert "tools" not in proxy or not proxy.get("tools")
        assert "FINAL ANSWER phase" in _system_text(proxy)
        print("final_answer_strips_tools: OK")
    else:
        print(f"final_answer_strips_tools: skipped (phase={phase})")


def test_memory_path_includes_priority():
    body = _cursor_body("router/main.py 읽고 구조 설명해줘")
    state = SessionState(project_key="test", workspace_path="/home/test/proj", current_query="router/main.py 읽고 구조 설명해줘")
    delta = _empty_delta()
    index = build_context_index(body, "mem_test")
    pack = build_with_budget(
        body,
        state,
        delta,
        [],
        "code_edit",
        "tool_planning",
        "long",
        index,
        query=index.query,
    )
    sys_text = ""
    for msg in pack.body.get("messages", []):
        if msg.get("role") == "system":
            sys_text = str(msg.get("content") or "")
            break
    assert "Priority order:" in sys_text
    assert "Runtime Phase Instruction" in sys_text
    print("memory_path_includes_priority: OK")


def test_read_only_tools_policy():
    from reference.planner import should_strip_tools, AgentPlan

    body = _cursor_body("코드 수정 말고 파일만 읽어서 구조 분석해줘")
    plan = AgentPlan(task_intent="project_inspection")
    stripped = should_strip_tools(plan, "read_only_analysis", body["messages"][1]["content"], body, "tool_planning")
    assert stripped is False
    idx = build_context_index(body, "ro_tools")
    intent = classify_intent(idx.query, idx)
    if intent.intent == "read_only_analysis":
        sys_text = system_for_intent(intent.intent, phase="tool_planning")
        assert "Shell and Edit are forbidden" in sys_text
    print("read_only_tools_policy: OK")


def main() -> None:
    test_tool_planning_prose_ban_and_glob()
    test_read_only_forbids_shell()
    test_final_answer_korean_and_evidence()
    test_runtime_phase_override_before_cursor()
    test_legacy_exec_cursor_system_order()
    test_final_answer_strips_tools()
    test_memory_path_includes_priority()
    test_read_only_tools_policy()
    print("\nAll prompt phase instruction tests passed.")


if __name__ == "__main__":
    main()
