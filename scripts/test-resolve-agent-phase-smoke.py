#!/usr/bin/env python3
"""Smoke: resolve_agent_phase must never raise on read_only hot paths."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from adapters.memory import SessionState  # noqa: E402
from reference.loop_guard import BAD_PING_PONG_TURNS  # noqa: E402
from reference.plan_state import resolve_agent_phase  # noqa: E402
from reference.planner import AgentPlan  # noqa: E402

STRUCTURE_QUERY = (
    "코드 수정 말고 router의 runtime_core, adapters, legacy, integrations 역할만 읽어서 요약해줘"
)


def _body(*, extra_user: str = "") -> dict:
    msgs = [
        {"role": "user", "content": STRUCTURE_QUERY},
        {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Glob", "arguments": "{}"}}]},
        {"role": "tool", "content": "Result of search in '/app/adapters' (total 8 files): gateway.py"},
    ]
    if extra_user:
        msgs.append({"role": "user", "content": extra_user})
    return {"messages": msgs, "tools": [{"type": "function", "function": {"name": "GlobSource"}}]}


def _read_only_plan(*, hits: list[str]) -> AgentPlan:
    return AgentPlan(
        router_intent="read_only_analysis",
        task_intent="project_inspection",
        goal=STRUCTURE_QUERY,
        evidence_needed=["target_coverage"],
        evidence_collected=["target_coverage"] if len(hits) >= 4 else [],
        source_hits=hits,
        preferred_sources=["dir.adapters", "dir.integrations", "dir.legacy", "dir.runtime_core"],
        source_registry={"sources": {}},
        final_ready=len(hits) >= 4,
        next_action={"tool": "GlobSource", "target": "dir.runtime_core", "reason": "collect"},
    )


def test_bad_ping_pong_incomplete_coverage_no_crash() -> None:
    plan = _read_only_plan(hits=["dir.adapters", "dir.integrations"])
    plan.evidence_collected = ["project_tree_seen"]
    state = SessionState(
        agent_plan=plan.to_dict(),
        turns_since_progress=BAD_PING_PONG_TURNS,
    )
    phase = resolve_agent_phase(_body(), state, STRUCTURE_QUERY, "read_only_analysis", True)
    assert phase in ("tool_planning", "partial_final_answer", "final_answer"), phase
    print("test_bad_ping_pong_incomplete_coverage_no_crash: OK")


def test_cursor_looping_flag_no_crash() -> None:
    plan = _read_only_plan(hits=["dir.adapters", "dir.integrations", "dir.legacy", "dir.runtime_core"])
    state = SessionState(agent_plan=plan.to_dict(), final_answer_count=1)
    phase = resolve_agent_phase(
        _body(extra_user="<system_reminder>Your messages have been flagged as looping."),
        state,
        STRUCTURE_QUERY,
        "read_only_analysis",
        True,
    )
    assert phase in ("final_answer", "partial_final_answer", "tool_planning"), phase
    print("test_cursor_looping_flag_no_crash: OK")


def test_after_tool_resolve_no_crash() -> None:
    plan = _read_only_plan(hits=["dir.adapters"])
    state = SessionState(agent_plan=plan.to_dict())
    phase = resolve_agent_phase(_body(), state, STRUCTURE_QUERY, "read_only_analysis", True)
    assert phase in ("tool_planning", "final_answer"), phase
    print("test_after_tool_resolve_no_crash: OK")


def main() -> int:
    tests = [
        test_bad_ping_pong_incomplete_coverage_no_crash,
        test_cursor_looping_flag_no_crash,
        test_after_tool_resolve_no_crash,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}", file=sys.stderr)
    print(f"smoke: passed={len(tests)-failed} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
