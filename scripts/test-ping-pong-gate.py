#!/usr/bin/env python3
"""Ping-pong / premature final_answer gate tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

from reference.agent_exec import detect_agent_phase, postprocess_agent_response
from reference.loop_guard import is_bad_ping_pong, reset_loop_counters, should_block_final_answer
from adapters.memory import SessionState
from reference.planner import AgentPlan, build_rule_plan, can_final_answer, normalize_plan


def test_general_requires_evidence():
    state = SessionState()
    q = "개선 방안 코드로 작성한다면 어떻게 될까 로그 확인해봐"
    plan = normalize_plan(build_rule_plan(q, state), q)
    assert plan.task_intent in ("log_analysis", "benchmark_analysis", "general")
    assert plan.evidence_needed, f"expected evidence_needed got {plan.evidence_needed}"
    assert not can_final_answer(plan)
    print("general_requires_evidence: OK")


def test_tool_result_not_auto_final():
    os.environ["MIN_TOOL_CALLS_FOR_FINAL_ANSWER"] = "1"
    q = "벤치마크 로그 분석해줘"
    state = SessionState()
    plan = normalize_plan(build_rule_plan(q, state), q)
    state.agent_plan = plan.to_dict()
    msgs = [
        {"role": "user", "content": q},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
        {"role": "tool", "content": "some partial data"},
    ]
    phase = detect_agent_phase({"messages": msgs, "tools": [{}]}, "code_edit", True, state, q)
    assert phase == "tool_planning", f"expected tool_planning got {phase}"
    print("tool_result_not_auto_final: OK")


def test_final_once_per_turn():
    state = SessionState()
    reset_loop_counters(state, "test query")
    plan = AgentPlan(
        task_intent="benchmark_analysis",
        evidence_needed=["runtime_score_seen", "agent_benchmark_seen"],
        evidence_collected=["runtime_score_seen:1", "agent_benchmark_seen:1"],
        next_action={"tool": "answer"},
    )
    state.final_answer_count = 1
    blocked, reason = should_block_final_answer(state, can_final=True, task_intent="benchmark_analysis")
    assert blocked and reason == "final_already_sent_this_turn"
    print("final_once_per_turn: OK")


def test_bad_ping_pong_xml_leaks():
    state = SessionState()
    state.xml_leak_count = 2
    assert is_bad_ping_pong(state)
    print("bad_ping_pong_xml_leaks: OK")


def test_xml_short_prose_promotes_tool():
    xml = (
        '<tool_call>\n<function=Read>\n<parameter=path>\n'
        "/home/yunahe/ai-runtime/cursor-local-llm/tmp/benchmark-runtime-score.json\n"
        "</parameter>\n</function>\n</tool_call>"
    )
    resp = {
        "choices": [{
            "message": {"role": "assistant", "content": xml},
            "finish_reason": "stop",
        }]
    }
    state = SessionState()
    state.xml_leak_count = 0
    out, log = postprocess_agent_response(
        resp, "code_edit", "벤치마크 로그", phase="final_answer", session_state=state
    )
    assert out["choices"][0].get("finish_reason") == "tool_calls" or out["choices"][0]["message"].get("tool_calls")
    assert state.xml_leak_count >= 1
    print("xml_short_prose_promotes_tool: OK")


def test_log_analysis_intent():
    q = "마지막 cursor 에서 llm에 입력된 로그 flow 구성 알려줘"
    state = SessionState()
    plan = normalize_plan(build_rule_plan(q, state), q)
    assert plan.task_intent == "log_analysis"
    assert "flow_phase_seen" in plan.evidence_needed
    print("log_analysis_intent: OK")


if __name__ == "__main__":
    test_general_requires_evidence()
    test_tool_result_not_auto_final()
    test_final_once_per_turn()
    test_bad_ping_pong_xml_leaks()
    test_xml_short_prose_promotes_tool()
    test_log_analysis_intent()
    print("all ping-pong gate tests passed")
