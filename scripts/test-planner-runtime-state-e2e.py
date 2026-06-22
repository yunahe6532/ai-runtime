#!/usr/bin/env python3
"""Phase 2.0 — Planner RuntimeState contract + shadow mode tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from agent_brain.planner_contract import PlannerDecision, tool_to_action  # noqa: E402
from agent_brain.planner_shadow import (  # noqa: E402
    compare_shadow_decisions,
    propose_shadow_decision,
    rule_decision_from_plan,
    run_planner_shadow,
)
from agent_brain.runtime_state import (  # noqa: E402
    MAX_RUNTIME_STATE_PROMPT_CHARS,
    RuntimeStateBuilder,
)
from legacy.memory_store import SessionState  # noqa: E402
from runtime_kernel.working_set import TurnWorkingSet  # noqa: E402


def _state(**kwargs) -> SessionState:
    s = SessionState()
    s.current_query = kwargs.get("query", "analyze router architecture")
    s.agent_plan = dict(kwargs.get("agent_plan") or {
        "task_intent": "project_inspection",
        "router_intent": "read_only_analysis",
        "evidence_needed": ["core_files_seen"],
        "evidence_collected": [],
        "next_action": {"tool": "Read", "target": "router/main.py", "reason": "entrypoint"},
        "confidence": 0.8,
    })
    s.task_journal = list(kwargs.get("task_journal") or [
        {"kind": "read", "target": "router/planner.py", "summary": "read planner"},
    ])
    s.evidence_anchors = list(kwargs.get("evidence_anchors") or [
        {"path": "router/planner.py", "line_start": 10, "summary": "AgentPlan"},
    ])
    s.handoff = dict(kwargs.get("handoff") or {"query": s.current_query, "anchor_count": 1})
    s.project_index = dict(kwargs.get("project_index") or {
        "file_count": 120,
        "entrypoints": ["router/main.py", "router/intent_router.py"],
    })
    s.last_working_set = dict(kwargs.get("last_working_set") or {
        "priority_targets": ["router/main.py"],
        "must_include": ["router/planner.py"],
    })
    return s


def test_runtime_state_builder_fields() -> None:
    state = _state()
    ws = TurnWorkingSet(
        priority_targets=list(state.last_working_set.get("priority_targets") or []),
        must_include=list(state.last_working_set.get("must_include") or []),
    )
    rs = RuntimeStateBuilder().build(
        session_state=state,
        query=state.current_query,
        phase="tool_planning",
        router_intent="read_only_analysis",
        context_intent="project_inspection",
        working_set=ws,
        coverage=SimpleNamespace(to_dict=lambda: {"complete": False, "coverage_score": 0.4}),
    )
    assert rs.current_user_request == state.current_query
    assert rs.router_intent
    assert rs.phase == "tool_planning"
    assert rs.project_index_summary.get("file_count") == 120
    assert rs.working_set_summary.get("priority_targets")
    assert len(rs.task_journal_tail) >= 1
    assert len(rs.evidence_anchor_summary) >= 1
    assert rs.handoff_summary.get("query")
    assert rs.runtime_self_model
    assert rs.available_actions
    assert "read" in rs.available_actions
    print("PASS test_runtime_state_builder_fields")


def test_prompt_compact_budget() -> None:
    state = _state()
    journal = [{"kind": "note", "target": f"t{i}", "summary": "x" * 200} for i in range(40)]
    state.task_journal = journal
    rs = RuntimeStateBuilder().build(session_state=state, query="q" * 500)
    prompt = rs.to_prompt_json(max_chars=2000)
    assert len(prompt) <= 2000 + 50
    assert "current_user_request" in prompt
    print("PASS test_prompt_compact_budget")


def test_planner_decision_json_parse() -> None:
    raw = json.dumps({
        "action": "read",
        "target_files": ["router/main.py"],
        "reason": "need entrypoint",
        "confidence": 0.9,
        "evidence_needed": ["core_files_seen"],
    })
    d = PlannerDecision.from_json(raw)
    assert d is not None
    assert d.action == "read"
    assert d.target_files == ["router/main.py"]
    assert d.confidence == 0.9
    legacy = PlannerDecision.from_dict({
        "action": "final_answer",
        "reasoning": "done",
        "tool_name": "answer",
    })
    assert legacy is not None
    assert legacy.action == "final"
    print("PASS test_planner_decision_json_parse")


def test_shadow_mode_no_hot_path_change() -> None:
    state = _state()
    ap_before = json.dumps(state.agent_plan, sort_keys=True)
    na_before = dict(state.agent_plan.get("next_action") or {})

    payload = run_planner_shadow(
        state,
        query=state.current_query,
        phase="tool_planning",
        router_intent="read_only_analysis",
        context_intent="project_inspection",
    )

    ap_after = json.dumps(state.agent_plan, sort_keys=True)
    na_after = dict(state.agent_plan.get("next_action") or {})
    assert ap_before == ap_after, "agent_plan must not change in shadow mode"
    assert na_before == na_after, "next_action must not change in shadow mode"
    assert payload.get("shadow_mode") is True
    assert payload.get("rule_decision")
    assert payload.get("shadow_decision")
    assert state.planner_runtime_state
    assert len(state.planner_runtime_state_prompt) <= MAX_RUNTIME_STATE_PROMPT_CHARS + 100
    print("PASS test_shadow_mode_no_hot_path_change")


def test_shadow_mismatch_detection() -> None:
    rule = PlannerDecision(action="read", target_files=["a.py"], reason="rule")
    shadow = PlannerDecision(action="grep", target_files=["b.py"], reason="shadow")
    cmp_ = compare_shadow_decisions(rule, shadow, phase="tool_planning")
    assert cmp_["match"] is False
    assert "action_mismatch" in cmp_["mismatch_reasons"]
    assert "target_mismatch" in cmp_["mismatch_reasons"]

    rule2 = PlannerDecision(action="final", target_files=[], reason="done")
    shadow2 = PlannerDecision(action="read", target_files=["x.py"], reason="not done")
    cmp2 = compare_shadow_decisions(rule2, shadow2, phase="final_answer")
    assert "phase_mismatch" in cmp2["mismatch_reasons"]
    print("PASS test_shadow_mismatch_detection")


def test_rule_decision_from_plan() -> None:
    ap = {"next_action": {"tool": "Grep", "pattern": "def main", "reason": "search"}, "confidence": 0.7}
    d = rule_decision_from_plan(ap, phase="tool_planning")
    assert d.action == "grep"
    assert tool_to_action("Shell") == "shell"
    print("PASS test_rule_decision_from_plan")


def test_inspector_runtime_state_section() -> None:
    from runtime_inspector import RuntimeInspectorContext, build_runtime_inspector

    state = _state()
    run_planner_shadow(state, query=state.current_query, phase="tool_planning")
    ctx = RuntimeInspectorContext(run_id="t", phase="tool_planning", session_state=state, agent_plan=state.agent_plan)
    md = build_runtime_inspector(ctx)
    assert "Planner RuntimeState" in md
    assert "Shadow Decision" in md
    print("PASS test_inspector_runtime_state_section")


def main() -> int:
    os.environ.setdefault("PLANNER_SHADOW_MODE", "1")
    test_runtime_state_builder_fields()
    test_prompt_compact_budget()
    test_planner_decision_json_parse()
    test_shadow_mode_no_hot_path_change()
    test_shadow_mismatch_detection()
    test_rule_decision_from_plan()
    test_inspector_runtime_state_section()
    print("\nAll Phase 2.0 planner runtime state tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
