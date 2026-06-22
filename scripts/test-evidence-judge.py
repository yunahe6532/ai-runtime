#!/usr/bin/env python3
"""Evidence Judge — batch static pre-eval + LLM sufficiency + guard tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

from reference.evidence_judge import (
    JudgeDecision,
    build_judge_input,
    evaluate_exploration,
    evaluate_exploration_static,
    phase_from_decision,
    should_run_judge_batch,
    validate_decision,
    _normalize_llm_decision,
)
from reference.evidence_store import append_evidence_item, build_evidence_item, evidence_items_for_judge
from reference.loop_guard import reset_loop_counters, should_block_final_answer
from adapters.memory import SessionState
from reference.planner import AgentPlan, normalize_plan, build_rule_plan


def test_evidence_item_accumulation():
    state = SessionState()
    item = build_evidence_item(
        tool="Read",
        path="router/plan_state.py",
        result_text="def resolve_agent_phase(): pass",
        tags=["code_location_seen:plan_state.py"],
        raw_ref="artifact_1",
    )
    append_evidence_item(state, item)
    assert len(state.evidence_items) == 1
    assert state.evidence_items[0]["evidence_type"] == "code_location_seen"
    append_evidence_item(state, item)
    assert len(state.evidence_items) == 1
    print("evidence_item_accumulation: OK")


def test_batch_judge_waits():
    os.environ["EVIDENCE_JUDGE_BATCH_SIZE"] = "2"
    state = SessionState()
    reset_loop_counters(state, "48턴 루프 원인 분석")
    plan = normalize_plan(
        build_rule_plan("48턴 ping-pong 원인과 튜닝 방향", state),
        "48턴 ping-pong 원인과 튜닝 방향",
    )
    plan.evidence_collected = ["flow_phase_seen:final 46/48"]
    state.agent_plan = plan.to_dict()
    state.tools_since_judge = 1

    static = evaluate_exploration_static(state, plan, query="48턴 ping-pong", tool_call_turns=1)
    assert not should_run_judge_batch(state, static, plan)

    _, decision = evaluate_exploration(state, plan, query="48턴 ping-pong", tool_call_turns=1)
    assert decision.source == "batch_wait"
    assert phase_from_decision(decision) == "tool_planning"
    print("batch_judge_waits: OK")


def test_batch_judge_runs_after_threshold():
    os.environ["EVIDENCE_JUDGE_BATCH_SIZE"] = "2"
    os.environ["EVIDENCE_JUDGE_MODE"] = "static"
    state = SessionState()
    plan = AgentPlan(
        task_intent="runtime_diagnosis",
        evidence_needed=["phase_distribution_seen", "loop_pattern_seen", "code_location_seen"],
        evidence_collected=["phase_distribution_seen:46/48", "loop_pattern_seen:xml leak"],
        goal="loop diagnosis",
    )
    state.agent_plan = plan.to_dict()
    state.tools_since_judge = 2
    state.files_read = [str(Path(__file__).resolve().parents[1] / "router" / "planner.py")]

    _, decision = evaluate_exploration(state, plan, query="loop diagnosis", tool_call_turns=2)
    assert decision.source != "batch_wait"
    assert decision.decision in ("continue_explore", "final_ready", "replan")
    assert state.judge_round == 1
    assert state.tools_since_judge == 0
    print("batch_judge_runs_after_threshold: OK")


def test_normalize_llm_schema():
    raw = {
        "sufficient": False,
        "confidence": 0.62,
        "reason": "planner 확인됐지만 plan_state write 미확인",
        "missing_evidence": ["plan_state write path"],
        "next_actions": [
            {"tool": "Grep", "query": "last_judge_decision", "reason": "저장 위치"},
            {"tool": "Grep", "query": "AgentPhase.FINAL", "reason": "final 진입"},
        ],
        "allow_final": False,
        "repair_needed": True,
    }
    dec = _normalize_llm_decision(raw)
    assert dec.decision == "replan"
    assert dec.repair_needed is True
    assert len(dec.next_actions) == 2
    assert dec.next_action["tool"] == "Grep"
    print("normalize_llm_schema: OK")


def test_guard_blocks_repeated_read():
    state = SessionState()
    path = "/home/yunahe/ai-runtime/cursor-local-llm/router/plan_state.py"
    state.files_read = [path]
    plan = AgentPlan(task_intent="runtime_diagnosis", goal="test")
    static = evaluate_exploration_static(state, plan, tool_call_turns=3)
    decision = JudgeDecision(
        decision="continue_explore",
        next_actions=[{"tool": "Read", "target": path, "reason": "repeat"}],
        next_action={"tool": "Read", "target": path},
        source="llm",
    )
    guarded = validate_decision(decision, static, state, plan)
    assert guarded.source == "guard_override"
    assert guarded.decision == "continue_explore"
    print("guard_blocks_repeated_read: OK")


def test_guard_blocks_final_on_ping_pong():
    state = SessionState()
    state.final_answer_count = 1
    plan = AgentPlan(
        task_intent="benchmark_analysis",
        evidence_collected=["runtime_score_seen:1"],
        evidence_needed=["runtime_score_seen"],
    )
    blocked, reason = should_block_final_answer(state, can_final=True, task_intent="benchmark_analysis")
    assert blocked and reason == "final_already_sent_this_turn"
    print("guard_blocks_final_on_ping_pong: OK")


def test_judge_input_uses_evidence_items():
    state = SessionState()
    append_evidence_item(
        state,
        build_evidence_item(
            tool="Grep",
            query="should_block_final_answer",
            result_text="loop_guard.py:def should_block_final_answer",
            tags=["fix_strategy_seen"],
        ),
    )
    plan = AgentPlan(task_intent="runtime_diagnosis", goal="guard 위치")
    static = evaluate_exploration_static(state, plan)
    inp = build_judge_input(state, plan, static, "guard 위치")
    assert inp["evidence_items"]
    assert inp["constraints"]["judge_does_not_execute_tools"] is True
    assert evidence_items_for_judge(state)[0]["tool"] == "Grep"
    print("judge_input_uses_evidence_items: OK")


def test_judge_llm_payload_has_max_tokens():
    from reference.evidence_judge import JUDGE_LLM_MAX_TOKENS, llm_evidence_judge

    assert JUDGE_LLM_MAX_TOKENS > 0
    # Offline: httpx may fail; ensure constant exists and function is callable.
    result = llm_evidence_judge({"query": "test", "evidence": []})
    assert result is None or hasattr(result, "decision")
    print("judge_llm_payload_has_max_tokens: OK")


def main():
    test_evidence_item_accumulation()
    test_batch_judge_waits()
    test_batch_judge_runs_after_threshold()
    test_judge_llm_payload_has_max_tokens()
    test_normalize_llm_schema()
    test_guard_blocks_repeated_read()
    test_guard_blocks_final_on_ping_pong()
    test_judge_input_uses_evidence_items()
    print("\nAll evidence judge tests passed.")


if __name__ == "__main__":
    main()
