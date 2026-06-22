#!/usr/bin/env python3
"""Recovery E2E — coverage fail → budget bump → re-retrieve → coverage pass."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

os.environ["RECOVERY_ENABLED"] = "1"
os.environ["MAX_RECOVERY_ROUNDS"] = "2"
os.environ["COVERAGE_THRESHOLD"] = "0.75"

from context_budget import BudgetPlan, allocate_dynamic  # noqa: E402
from context_need import ContextNeed  # noqa: E402
from coverage_checker import check_coverage  # noqa: E402
from reference.loop_guard import should_block_final_answer  # noqa: E402
from adapters.memory import SessionState  # noqa: E402
from prompt_builder import PromptPack  # noqa: E402
from recovery_scheduler import RecoveryScheduler  # noqa: E402
from adapters.retrieval import RetrievalItem, RetrievalPack  # noqa: E402


THRESHOLD = 0.75


def _fail_pack() -> PromptPack:
    text = """
[Must Include]
- current user request
- active agent plan
[Task]
fix context budget bug
[Saved Agent Plan]
task_intent: bugfix
{"role": "tool", "content": "partial grep — missing allocate_dynamic body"}
"""
    return PromptPack(
        body={"messages": [{"role": "system", "content": text}]},
        phase="tool_planning",
        must_include_block="[Must Include]\n- current user request",
        truncation_markers=[
            {
                "source": "context_budget.py",
                "reason": "budget_exceeded",
                "lost_tokens": 1200,
                "critical": True,
            }
        ],
    )


def _pass_pack() -> PromptPack:
    text = """
[Must Include]
- current user request
- active agent plan
[Saved Agent Plan]
task_intent: bugfix
def normalize_plan
def allocate_dynamic
def build_with_budget
context_budget.py allocate_dynamic
prompt_builder.py build_with_budget
{"role": "tool", "content": "grep hit"}
[Task]
fix context budget bug
"""
    return PromptPack(
        body={"messages": [{"role": "system", "content": text}]},
        phase="tool_planning",
        must_include_block="[Must Include]\n- current user request",
    )


def _empty_pack() -> RetrievalPack:
    return RetrievalPack(items=[], total_tokens=0, missing_targets=["context_budget.py"])


def _full_pack() -> RetrievalPack:
    content = """
def normalize_plan(plan, query):
    pass

def allocate_dynamic(backend, phase, max_output, need, stats):
    pass

def build_with_budget(body, state, delta):
    pass
"""
    return RetrievalPack(
        items=[
            RetrievalItem(
                source="context_budget.py",
                tokens=400,
                score=0.95,
                section="file_read",
                must_include=True,
                content=content,
            ),
            RetrievalItem(
                source="prompt_builder.py",
                tokens=300,
                score=0.9,
                section="file_read",
                content="def build_with_budget(...): ...",
            ),
        ],
        total_tokens=700,
        missing_targets=[],
    )


def test_recovery_e2e():
    need = ContextNeed(
        intent="bugfix",
        required_sources=["retrieved_code", "tool_result"],
        must_include=["current user request", "active agent plan", "latest tool result"],
        coverage_targets=[
            "context_budget.py",
            "prompt_builder.py",
            "context_budget.py::allocate_dynamic",
            "prompt_builder.py::build_with_budget",
        ],
        priority={"retrieved": 0.5, "artifact": 0.3},
    )

    budget = allocate_dynamic("long", "tool_planning", 800, need, _empty_pack())
    fail_prompt = _fail_pack()
    coverage_before = check_coverage(need, _empty_pack(), fail_prompt)

    assert not coverage_before.complete
    assert coverage_before.coverage_score < THRESHOLD

    state = SessionState()
    state.agent_plan = {"task_intent": "bugfix", "evidence_needed": [], "evidence_collected": []}
    state.last_runtime_turn = {
        "coverage_complete": False,
        "coverage_score": coverage_before.coverage_score,
        "recovery_triggered": False,
        "recovery_recovered": False,
        "critical_source_truncated": coverage_before.critical_source_truncated,
    }
    state.last_ingest_metrics = {
        "coverage_complete": False,
        "coverage_score": coverage_before.coverage_score,
    }
    blocked_before, reason_before = should_block_final_answer(
        state, can_final=True, task_intent="bugfix", intent_name="agent",
    )
    assert blocked_before, reason_before

    round_num = [0]

    def mock_retrieve(**kwargs):
        round_num[0] += 1
        return _full_pack() if round_num[0] >= 1 else _empty_pack()

    def mock_build(**kwargs):
        if round_num[0] >= 1:
            return _pass_pack()
        return _fail_pack()

    scheduler = RecoveryScheduler()
    result = scheduler.recover(
        context_need=need,
        budget=budget,
        retrieval_pack=_empty_pack(),
        coverage=coverage_before,
        retrieve_fn=lambda **kw: mock_retrieve(**kw),
        build_fn=lambda **kw: mock_build(**kw),
        retrieve_kwargs={"state": state, "query": "fix bug", "delta": None, "need": need},
        build_kwargs={"state": state},
    )

    assert result.rounds >= 1, "recovery should run at least one round"
    coverage_after = result.coverage or check_coverage(need, result.retrieval_pack, result.prompt_pack)
    assert coverage_after.coverage_score >= THRESHOLD, coverage_after.to_dict()
    assert coverage_after.complete, coverage_after.to_dict()

    state.last_runtime_turn = {
        "coverage_complete": True,
        "coverage_score": coverage_after.coverage_score,
        "recovery_triggered": True,
        "recovery_recovered": True,
        "recovery_rounds": result.rounds,
        "critical_source_truncated": False,
        "latest_tool_result_missing": False,
    }
    state.last_ingest_metrics = {
        "coverage_complete": True,
        "coverage_score": coverage_after.coverage_score,
        "recovery_recovered": True,
    }
    blocked_after, _ = should_block_final_answer(
        state, can_final=True, task_intent="bugfix", intent_name="agent",
    )
    assert not blocked_after, "final should be allowed after recovery"

    print(
        "recovery_e2e: OK",
        f"before={coverage_before.coverage_score:.2f}",
        f"after={coverage_after.coverage_score:.2f}",
        f"rounds={result.rounds}",
        f"blocked_before={reason_before or blocked_before}",
    )


if __name__ == "__main__":
    test_recovery_e2e()
    print("all passed")
