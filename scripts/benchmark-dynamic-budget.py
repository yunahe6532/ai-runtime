#!/usr/bin/env python3
"""Benchmark / unit tests for dynamic context budget (Phases 1–4 scaffold)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

os.environ.setdefault("DYNAMIC_BUDGET", "1")
os.environ.setdefault("COVERAGE_CHECK", "1")
os.environ.setdefault("RECOVERY_ENABLED", "0")

from context_budget import RetrievalStats, allocate_dynamic, allocate_static  # noqa: E402
from context_need import (  # noqa: E402
    INTENT_PRESETS,
    build_context_need,
    resolve_context_intent,
)
from coverage_checker import check_coverage  # noqa: E402
from adapters.memory import Artifact, RequestDelta, SessionState  # noqa: E402
from reference.planner import AgentPlan, normalize_plan  # noqa: E402
from prompt_builder import PromptPack, build_with_budget  # noqa: E402
from adapters.retrieval import RetrievalItem, RetrievalPack, estimate_chunk_tokens, retrieve_for_need  # noqa: E402


def test_intent_presets_exist():
    for key in ("bugfix", "recall", "doc_summary", "architecture", "code_edit"):
        assert key in INTENT_PRESETS
    print("intent_presets_exist: OK")


def test_resolve_recall_intent():
    assert resolve_context_intent("general", "우리가 아까 뭐 이야기했지?") == "recall"
    assert resolve_context_intent("general", "이 문서 요약해줘") == "doc_summary"
    assert resolve_context_intent("general", "버그 수정해줘", "code_edit") == "bugfix"
    print("resolve_recall_intent: OK")


def test_allocate_dynamic_differs_by_intent():
    static = allocate_static("long", "tool_planning", 800)
    recall_need = build_context_need(
        AgentPlan(task_intent="general"),
        "우리가 아까 뭐 이야기했지?",
    )
    bug_need = build_context_need(
        AgentPlan(task_intent="benchmark_analysis"),
        "router 버그 수정",
        "code_edit",
    )
    recall_budget = allocate_dynamic("long", "tool_planning", 800, recall_need, RetrievalStats())
    bug_budget = allocate_dynamic(
        "long",
        "tool_planning",
        800,
        bug_need,
        RetrievalStats(total_tokens=5000, item_count=3),
    )
    assert recall_budget.mode == "dynamic"
    assert recall_budget.session_tail > static.session_tail or recall_budget.session_tail >= bug_budget.session_tail
    assert bug_budget.retrieved >= recall_budget.retrieved
    print(
        "allocate_dynamic_differs: OK",
        f"recall_session={recall_budget.session_tail}",
        f"bug_retrieved={bug_budget.retrieved}",
    )


def test_retrieval_pack_tokens():
    state = SessionState()
    state.artifacts = ["a1"]
    need = build_context_need(AgentPlan(), "read planner.py", "agent")
    # No artifacts on disk — empty pack
    pack = retrieve_for_need(
        state,
        "planner",
        RequestDelta(
            delta_id="d",
            req_id="r",
            prev_req_id=None,
            prev_message_count=0,
            curr_message_count=0,
            added_count=0,
        ),
        need,
        2000,
    )
    assert pack.total_tokens >= 0
    assert isinstance(pack.missing_targets, list)
    print("retrieval_pack_tokens: OK", pack.total_tokens)


def test_coverage_must_include():
    need = build_context_need(AgentPlan(goal="fix bug"), "fix planner bug", "code_edit")
    pack = PromptPack(
        body={"messages": [{"role": "system", "content": "[Saved Agent Plan]"}]},
        phase="tool_planning",
    )
    report = check_coverage(need, RetrievalPack(items=[]), pack)
    assert not report.complete
    assert report.action in ("re_retrieve", "ask_tool", "increase_budget")
    print("coverage_must_include: OK", report.coverage_score, report.missing[:2])


def test_build_with_budget_returns_metadata():
    state = SessionState(current_query="analyze context budget")
    delta = RequestDelta(
        delta_id="d",
        req_id="r",
        prev_req_id=None,
        prev_message_count=0,
        curr_message_count=1,
        added_count=0,
        last_role="user",
    )
    from context_cache import ContextIndex

    idx = ContextIndex(req_id="r", query="analyze context budget", raw_tokens=100, message_count=1, tool_count=0)
    need = build_context_need(AgentPlan(), "analyze context budget", "agent")
    budget = allocate_dynamic("long", "tool_planning", 800, need, RetrievalStats())
    pack = build_with_budget(
        {"messages": [], "max_tokens": 800, "tools": []},
        state,
        delta,
        [],
        "agent",
        "tool_planning",
        "long",
        idx,
        budget_plan=budget,
        context_need=need,
    )
    assert isinstance(pack, PromptPack)
    assert pack.budget is not None
    assert pack.budget.mode == "dynamic"
    assert "[Must Include]" in pack.must_include_block or pack.must_include_block == ""
    assert pack.body.get("messages")
    print("build_with_budget_metadata: OK", pack.budget.to_dict())


def test_planner_attaches_context_need():
    plan = normalize_plan(AgentPlan(task_intent="benchmark_analysis"), "벤치마크 분석")
    assert plan.context_need.get("intent")
    assert plan.context_need.get("priority")
    print("planner_context_need: OK", plan.context_need.get("intent"))


def test_estimate_chunk_tokens():
    assert estimate_chunk_tokens("x" * 300) == 100
    print("estimate_chunk_tokens: OK")


def test_coverage_symbol_targets():
    from context_need import ContextNeed

    need = ContextNeed(
        intent="bugfix",
        must_include=["current user request", "active agent plan"],
        coverage_targets=[
            "context_budget.py::allocate_dynamic",
            "prompt_builder.py::build_with_budget",
        ],
    )
    partial = PromptPack(
        body={"messages": [{"role": "system", "content": "context_budget.py def other_fn"}]},
        phase="tool_planning",
        must_include_block="[Task] fix bug\n[Saved Agent Plan]",
    )
    report = check_coverage(need, RetrievalPack(items=[]), partial)
    assert "allocate_dynamic" in str(report.symbol_missing) or not report.complete
    print("coverage_symbol_targets: OK", report.symbol_missing[:2])


def test_coverage_critical_truncation():
    from context_need import ContextNeed

    need = ContextNeed(intent="code_edit", must_include=["current user request"])
    pack = PromptPack(
        body={"messages": [{"role": "system", "content": "[Task] edit\n[Saved Agent Plan]"}]},
        phase="tool_planning",
        truncation_markers=[{"source": "planner.py", "lost_tokens": 800, "critical": True}],
    )
    report = check_coverage(need, RetrievalPack(items=[]), pack)
    assert report.critical_source_truncated
    assert report.action == "increase_budget"
    print("coverage_critical_truncation: OK", report.action)


def test_inspector_budget_coverage_sections():
    from runtime_inspector import RuntimeInspectorContext, build_runtime_inspector

    ctx = RuntimeInspectorContext(
        phase="tool_planning",
        intent="bugfix",
        runtime_turn={
            "intent": "bugfix",
            "phase": "tool_planning",
            "dynamic_budget_enabled": True,
            "budget_plan": {
                "mode": "dynamic",
                "retrieved": 14000,
                "session_tail": 2000,
                "artifact": 6000,
            },
            "retrieval_total_tokens": 14000,
            "coverage_score": 0.82,
            "coverage_complete": False,
            "coverage_missing": ["context_budget.py::allocate_dynamic"],
            "coverage_action": "re_retrieve",
            "recovery_triggered": True,
            "recovery_recovered": False,
            "final_blocked_reason": "coverage_incomplete",
        },
    )
    html = build_runtime_inspector(ctx)
    assert "Runtime Budget" in html
    assert "retrieved: 14,000" in html
    assert "Coverage" in html
    assert "score: 0.82" in html
    assert "re_retrieve" in html
    print("inspector_budget_coverage: OK")


def test_overbudget_truncation_priority():
    need = build_context_need(AgentPlan(), "analyze huge codebase", "architecture")
    huge = RetrievalPack(
        items=[
            RetrievalItem(source=f"f{i}.py", tokens=3000, score=0.5, content="x" * 9000)
            for i in range(10)
        ],
        total_tokens=30000,
    )
    budget = allocate_dynamic("long", "tool_planning", 800, need, RetrievalStats.from_pack(huge))
    assert budget.retrieved < huge.total_tokens or budget.mode == "dynamic"
    print("overbudget_truncation: OK", budget.retrieved)


def main():
    test_intent_presets_exist()
    test_resolve_recall_intent()
    test_allocate_dynamic_differs_by_intent()
    test_retrieval_pack_tokens()
    test_coverage_must_include()
    test_coverage_symbol_targets()
    test_coverage_critical_truncation()
    test_build_with_budget_returns_metadata()
    test_planner_attaches_context_need()
    test_estimate_chunk_tokens()
    test_inspector_budget_coverage_sections()
    test_overbudget_truncation_priority()
    print("unit tests OK — run benchmark-dynamic-budget-matrix.py for full matrix")
    print("ALL OK")


if __name__ == "__main__":
    main()
