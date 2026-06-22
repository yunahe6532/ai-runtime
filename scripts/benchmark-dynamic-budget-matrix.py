#!/usr/bin/env python3
"""Dynamic budget scenario matrix — 25+ regression cases."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

os.environ.setdefault("DYNAMIC_BUDGET", "1")
os.environ.setdefault("COVERAGE_CHECK", "1")
os.environ.setdefault("RECOVERY_ENABLED", "0")
os.environ.setdefault("VECTOR_RETRIEVAL", "1")

from context_budget import RetrievalStats, allocate_dynamic, allocate_static  # noqa: E402
from context_need import ContextNeed, build_context_need, resolve_context_intent  # noqa: E402
from coverage_checker import check_coverage  # noqa: E402
from integrations.llamaindex import _builtin_vector_search, vector_retrieval_enabled  # noqa: E402
from adapters.memory import RequestDelta, SessionState  # noqa: E402
from reference.planner import AgentPlan  # noqa: E402
from prompt_builder import PromptPack  # noqa: E402
from adapters.retrieval import RetrievalItem, RetrievalPack, estimate_chunk_tokens, retrieve_for_need  # noqa: E402

DELTA = RequestDelta(
    delta_id="d", req_id="r", prev_req_id=None,
    prev_message_count=0, curr_message_count=1, added_count=1,
)


def _need(intent: str, query: str, route: str = "agent") -> ContextNeed:
    return build_context_need(AgentPlan(task_intent=intent), query, route)


def _budget(intent: str, query: str, stats: RetrievalStats | None = None) -> dict:
    need = _need(intent, query)
    b = allocate_dynamic("long", "tool_planning", 800, need, stats or RetrievalStats())
    return {"intent": need.intent, "retrieved": b.retrieved, "session_tail": b.session_tail, "artifact": b.artifact}


SCENARIOS = [
    # intent resolution (6)
    ("resolve_recall", lambda: resolve_context_intent("general", "우리가 아까 뭐 이야기했지?") == "recall"),
    ("resolve_doc_summary", lambda: resolve_context_intent("general", "이 문서 요약해줘") == "doc_summary"),
    ("resolve_bugfix", lambda: resolve_context_intent("general", "버그 수정", "code_edit") == "bugfix"),
    ("resolve_architecture", lambda: resolve_context_intent("project_inspection", "explain structure") == "architecture"),
    ("resolve_code_edit", lambda: resolve_context_intent("general", "planner.py line 10", "agent") == "code_edit"),
    ("resolve_general", lambda: resolve_context_intent("general", "hello") in ("general", "recall", "doc_summary")),
    # budget by intent (8)
    ("budget_recall_session_high", lambda: _budget("general", "아까 뭐 이야기했지")["session_tail"] > _budget("bugfix", "fix bug")["session_tail"]),
    ("budget_bugfix_retrieved_high", lambda: _budget("code_edit", "fix router bug")["retrieved"] >= _budget("general", "hi")["retrieved"]),
    ("budget_architecture_artifact", lambda: _budget("architecture", "system design")["artifact"] > 0),
    ("budget_doc_summary_retrieved", lambda: _budget("doc_summary", "summarize paper")["retrieved"] > 0),
    ("budget_code_edit_retrieved", lambda: _budget("code_edit", "edit file")["retrieved"] > _budget("recall", "remember")["retrieved"] - 1000),
    ("budget_overbudget_caps", lambda: allocate_dynamic("long", "tool_planning", 800, _need("architecture", "huge"), RetrievalStats(total_tokens=50000)).retrieved < 50000),
    ("budget_static_mode", lambda: allocate_static("long", "tool_planning", 800).mode == "static"),
    ("budget_dynamic_mode", lambda: allocate_dynamic("long", "tool_planning", 800, _need("bugfix", "x"), RetrievalStats()).mode == "dynamic"),
    # coverage (7)
    ("cov_must_include_fail", lambda: not check_coverage(_need("code_edit", "fix"), RetrievalPack(items=[]), PromptPack(body={"messages": [{"role": "system", "content": ""}]}, phase="tool_planning")).complete),
    ("cov_symbol_missing", lambda: "allocate_dynamic" in str(check_coverage(ContextNeed(intent="bugfix", coverage_targets=["context_budget.py::allocate_dynamic"]), RetrievalPack(items=[]), PromptPack(body={"messages": [{"role": "system", "content": "x"}]}, phase="tool_planning")).symbol_missing)),
    ("cov_truncation_action", lambda: check_coverage(ContextNeed(intent="code_edit", must_include=["current user request"]), RetrievalPack(items=[]), PromptPack(body={"messages": [{"role": "system", "content": "[Task] x"}]}, phase="tool_planning", truncation_markers=[{"source": "a.py", "critical": True, "lost_tokens": 500}])).action == "increase_budget"),
    ("cov_pass_full", lambda: check_coverage(ContextNeed(intent="bugfix", must_include=["current user request", "active agent plan"], coverage_targets=[]), RetrievalPack(items=[]), PromptPack(body={"messages": [{"role": "system", "content": "[Task] x\n[Saved Agent Plan] y"}]}, phase="tool_planning", must_include_block="[Task]")).coverage_score >= 0.75),
    ("cov_evidence_missing", lambda: "evidence:" in str(check_coverage(ContextNeed(intent="bugfix"), RetrievalPack(items=[]), PromptPack(body={"messages": [{"role": "system", "content": "[Saved Agent Plan]"}]}, phase="tool_planning"), evidence_needed=["readme_seen"], evidence_collected=[]).missing)),
    ("cov_latest_tool_missing", lambda: check_coverage(ContextNeed(intent="bugfix", must_include=["latest tool result"]), RetrievalPack(items=[]), PromptPack(body={"messages": [{"role": "system", "content": "[Saved Agent Plan]"}]}, phase="tool_planning")).latest_tool_result_missing),
    ("cov_two_file_bugfix", lambda: len(check_coverage(ContextNeed(intent="bugfix", coverage_targets=["context_budget.py", "prompt_builder.py"]), RetrievalPack(items=[]), PromptPack(body={"messages": [{"role": "system", "content": "only one"}]}, phase="tool_planning")).missing) >= 1),
    # retrieval / vector (4)
    ("retrieve_empty_no_artifacts", lambda: retrieve_for_need(SessionState(), "q", DELTA, _need("bugfix", "q"), 2000).total_tokens == 0),
    ("estimate_tokens", lambda: estimate_chunk_tokens("a" * 300) == 100),
    ("vector_enabled_flag", lambda: vector_retrieval_enabled() is True),
    ("vector_bm25_rank", lambda: _builtin_vector_search([{"text": "allocate_dynamic budget plan", "source": "a.py"}, {"text": "unrelated hello world", "source": "b.py"}], "budget allocate", top_k=1)[0][1]["source"] == "a.py"),
]


def run_matrix() -> tuple[int, int, list[str]]:
    passed = 0
    failed: list[str] = []
    for name, fn in SCENARIOS:
        try:
            ok = bool(fn())
            if ok:
                passed += 1
                print(f"  OK  {name}")
            else:
                failed.append(name)
                print(f"  FAIL {name}")
        except Exception as exc:
            failed.append(f"{name}: {exc}")
            print(f"  ERR  {name}: {exc}")
    return passed, len(SCENARIOS), failed


def main():
    print(f"dynamic budget matrix ({len(SCENARIOS)} cases)")
    passed, total, failed = run_matrix()
    print(f"\n{passed}/{total} passed")
    if failed:
        print("failed:", ", ".join(failed[:10]))
        sys.exit(1)
    print("ALL OK")


if __name__ == "__main__":
    main()
