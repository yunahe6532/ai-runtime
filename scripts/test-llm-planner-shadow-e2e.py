#!/usr/bin/env python3
"""Phase 2.1 — LLM Planner shadow mode E2E tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from agent_brain.llm_planner import propose_llm_shadow_decision  # noqa: E402
from agent_brain.planner_shadow import compare_triple_decisions, run_planner_shadow  # noqa: E402
from agent_brain.runtime_state import RuntimeState, RuntimeStateBuilder  # noqa: E402
from legacy.memory_store import SessionState  # noqa: E402


def _runtime_state() -> RuntimeState:
    state = SessionState()
    state.current_query = "analyze router"
    state.agent_plan = {
        "next_action": {"tool": "Read", "target": "router/main.py", "reason": "entry"},
        "evidence_needed": ["core_files_seen"],
        "evidence_collected": [],
        "confidence": 0.8,
    }
    state.project_index = {"file_count": 5, "entrypoints": ["router/main.py"]}
    state.last_working_set = {"priority_targets": ["router/main.py"], "must_include": []}
    return RuntimeStateBuilder().build(
        session_state=state,
        query=state.current_query,
        phase="tool_planning",
        router_intent="read_only_analysis",
        coverage=SimpleNamespace(to_dict=lambda: {"complete": False, "coverage_score": 0.3}),
    )


def test_json_parse_success(tmp: Path) -> None:
    os.environ["EXPLORER_TRACE_PATH"] = str(tmp / "t.ndjson")
    os.environ["EXPLORER_TRACE_STDOUT"] = "0"
    rs = _runtime_state()

    def fake_invoke(_prompt: str) -> tuple[str, dict]:
        return (
            json.dumps({
                "action": "read",
                "target_files": ["router/main.py"],
                "reason": "need entrypoint",
                "confidence": 0.88,
                "evidence_needed": ["core_files_seen"],
                "risk_flags": [],
            }),
            {"status": "ok"},
        )

    dec, meta = propose_llm_shadow_decision(rs, _invoke=fake_invoke)
    assert meta["status"] == "ok"
    assert dec.action == "read"
    assert dec.target_files == ["router/main.py"]
    assert dec.confidence == 0.88
    print("PASS test_json_parse_success")


def test_malformed_json_fallback() -> None:
    rs = _runtime_state()
    dec, meta = propose_llm_shadow_decision(rs, _invoke=lambda _p: ("not json at all", {"status": "ok"}))
    assert meta["status"] == "parse_fail"
    assert dec.action in ("recover", "ask_user")
    assert "parse_fail" in dec.risk_flags
    print("PASS test_malformed_json_fallback")


def test_invalid_action_fallback() -> None:
    rs = _runtime_state()
    dec, meta = propose_llm_shadow_decision(
        rs,
        _invoke=lambda _p: (json.dumps({"action": "fly_to_moon", "reason": "x"}), {"status": "ok"}),
    )
    assert meta["status"] == "invalid_action"
    assert dec.action == "recover"
    print("PASS test_invalid_action_fallback")


def test_timeout_fallback() -> None:
    rs = _runtime_state()
    dec, meta = propose_llm_shadow_decision(rs, _invoke=lambda _p: ("", {"status": "timeout"}))
    assert meta["status"] == "timeout"
    assert dec.action == "recover"
    assert "timeout" in dec.risk_flags
    print("PASS test_timeout_fallback")


def test_hot_path_unchanged_with_llm_enabled(tmp: Path) -> None:
    os.environ["EXPLORER_TRACE_PATH"] = str(tmp / "hot.ndjson")
    os.environ["EXPLORER_TRACE_STDOUT"] = "0"
    os.environ["PLANNER_SHADOW_MODE"] = "1"
    os.environ["LLM_PLANNER_SHADOW_ENABLED"] = "1"

    state = SessionState()
    state.current_query = "analyze router"
    state.agent_plan = {
        "next_action": {"tool": "Read", "target": "router/main.py", "reason": "entry"},
        "evidence_needed": ["core_files_seen"],
        "evidence_collected": [],
        "confidence": 0.8,
    }
    state.project_index = {"file_count": 5, "entrypoints": ["router/main.py"]}
    state.last_working_set = {"priority_targets": ["router/main.py"], "must_include": []}
    ap_before = json.dumps(state.agent_plan, sort_keys=True)

    with mock.patch(
        "agent_brain.llm_planner._invoke_llm",
        return_value=(
            json.dumps({
                "action": "grep",
                "target_files": [],
                "reason": "different from rule",
                "confidence": 0.6,
            }),
            {"status": "ok"},
        ),
    ):
        payload = run_planner_shadow(
            state,
            query=state.current_query,
            phase="tool_planning",
            router_intent="read_only_analysis",
            coverage=SimpleNamespace(to_dict=lambda: {"complete": False}),
        )

    assert json.dumps(state.agent_plan, sort_keys=True) == ap_before
    assert payload.get("llm_shadow_decision")
    assert payload.get("triple_comparison")
    assert payload["triple_comparison"]["llm_action"] == "grep"
    assert payload["triple_comparison"]["rule_action"] == "read"
    print("PASS test_hot_path_unchanged_with_llm_enabled")


def test_explorer_trace_llm_and_triple(tmp: Path) -> None:
    trace = tmp / "llm.ndjson"
    os.environ["EXPLORER_TRACE_PATH"] = str(trace)
    os.environ["EXPLORER_TRACE_STDOUT"] = "0"
    os.environ["PLANNER_SHADOW_MODE"] = "1"
    os.environ["LLM_PLANNER_SHADOW_ENABLED"] = "1"

    state = SessionState()
    state.current_query = "q"
    state.agent_plan = {
        "next_action": {"tool": "Read", "target": "a.py", "reason": "r"},
        "evidence_needed": [],
        "evidence_collected": [],
    }
    state.project_index = {"entrypoints": ["a.py"]}
    state.last_working_set = {"priority_targets": ["a.py"]}

    with mock.patch(
        "agent_brain.llm_planner._invoke_llm",
        return_value=(
            json.dumps({"action": "read", "target_files": ["a.py"], "reason": "ok", "confidence": 0.9}),
            {"status": "ok"},
        ),
    ):
        run_planner_shadow(state, query="q", phase="tool_planning")

    events = {json.loads(ln)["event"] for ln in trace.read_text(encoding="utf-8").splitlines() if ln.strip()}
    assert "planner.llm.proposed" in events
    assert "planner.triple_compared" in events
    print("PASS test_explorer_trace_llm_and_triple")


def test_triple_comparison_fields() -> None:
    from agent_brain.planner_contract import PlannerDecision

    rule = PlannerDecision(action="read", target_files=["a.py"], confidence=0.7)
    heur = PlannerDecision(action="read", target_files=["a.py"], confidence=0.75)
    llm = PlannerDecision(action="grep", target_files=["b.py"], confidence=0.6, risk_flags=["explore"])
    triple = compare_triple_decisions(rule, heur, llm, phase="tool_planning")
    assert triple["action_match_rule_heuristic"] is True
    assert triple["action_match_rule_llm"] is False
    assert triple["llm_action"] == "grep"
    assert triple["risk_flags_llm"] == ["explore"]
    print("PASS test_triple_comparison_fields")


def main() -> int:
    os.environ.setdefault("EXPLORER_TRACE_ENABLED", "1")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_json_parse_success(tmp)
        test_malformed_json_fallback()
        test_invalid_action_fallback()
        test_timeout_fallback()
        test_hot_path_unchanged_with_llm_enabled(tmp)
        test_explorer_trace_llm_and_triple(tmp)
    test_triple_comparison_fields()
    print("\nAll LLM planner shadow E2E tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
