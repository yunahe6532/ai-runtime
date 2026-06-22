#!/usr/bin/env python3
"""Phase 2.2a — Planner promotion gate E2E tests."""

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

from agent_brain.planner_contract import PlannerDecision  # noqa: E402
from agent_brain.planner_shadow import run_planner_shadow  # noqa: E402
from agent_brain.promotion_gate import (  # noqa: E402
    evaluate_promotion,
    promotion_metrics_snapshot,
    reset_promotion_metrics,
)
from agent_brain.runtime_state import RuntimeState, RuntimeStateBuilder  # noqa: E402
from legacy.memory_store import SessionState  # noqa: E402


def _runtime_state(*, router_intent: str = "read_only_analysis") -> RuntimeState:
    state = SessionState()
    state.current_query = "analyze router structure"
    state.agent_plan = {
        "next_action": {"tool": "Read", "target": "router/main.py", "reason": "entry"},
        "router_intent": router_intent,
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
        router_intent=router_intent,
        coverage=SimpleNamespace(to_dict=lambda: {"complete": False, "coverage_score": 0.3}),
    )


def _dec(
    action: str,
    *,
    targets: list[str] | None = None,
    confidence: float = 0.88,
    risk_flags: list[str] | None = None,
    tool_args: dict | None = None,
) -> PlannerDecision:
    return PlannerDecision(
        action=action,
        target_files=targets or ["router/main.py"],
        confidence=confidence,
        risk_flags=risk_flags or [],
        tool_args=tool_args or {},
        reason=f"test {action}",
    )


def test_read_eligible() -> None:
    rs = _runtime_state()
    rule = _dec("read")
    heuristic = _dec("read")
    llm = _dec("read", confidence=0.9)
    promo = evaluate_promotion(rule, heuristic, llm, rs)
    assert promo.eligible is True
    assert promo.allowed_action == "read"
    assert promo.shadow_only is True
    assert promo.dry_run_tool_call.get("function", {}).get("name") == "ReadSource"
    print("PASS test_read_eligible")


def test_grep_eligible_via_target_overlap() -> None:
    rs = _runtime_state()
    rule = _dec("read", targets=["router/main.py"])
    heuristic = _dec("read", targets=["router/main.py"])
    llm = _dec("grep", targets=["router/main.py"], tool_args={"pattern": "class |def"})
    promo = evaluate_promotion(rule, heuristic, llm, rs)
    assert promo.eligible is True
    assert promo.allowed_action == "grep"
    assert promo.dry_run_tool_call.get("function", {}).get("name") == "GrepSource"
    print("PASS test_grep_eligible_via_target_overlap")


def test_glob_eligible() -> None:
    rs = _runtime_state()
    rule = _dec("glob", targets=["dir.runtime_core"])
    heuristic = _dec("glob", targets=["dir.runtime_core"])
    llm = _dec("glob", targets=["dir.runtime_core"], tool_args={"glob_pattern": "*.py"})
    promo = evaluate_promotion(rule, heuristic, llm, rs)
    assert promo.eligible is True
    assert promo.allowed_action == "glob"
    assert promo.dry_run_tool_call.get("function", {}).get("name") == "GlobSource"
    print("PASS test_glob_eligible")


def test_edit_blocked() -> None:
    rs = _runtime_state()
    rule = _dec("read")
    heuristic = _dec("read")
    llm = _dec("edit")
    promo = evaluate_promotion(rule, heuristic, llm, rs)
    assert promo.eligible is False
    assert promo.allowed_action == "none"
    assert any("blocked_by_action" in b for b in promo.blocked_reasons)
    print("PASS test_edit_blocked")


def test_shell_blocked() -> None:
    rs = _runtime_state()
    promo = evaluate_promotion(_dec("read"), _dec("read"), _dec("shell"), rs)
    assert promo.eligible is False
    assert any("blocked_by_action:shell" in b for b in promo.blocked_reasons)
    print("PASS test_shell_blocked")


def test_final_blocked() -> None:
    rs = _runtime_state()
    promo = evaluate_promotion(_dec("read"), _dec("read"), _dec("final"), rs)
    assert promo.eligible is False
    assert any("blocked_by_action:final" in b for b in promo.blocked_reasons)
    print("PASS test_final_blocked")


def test_low_confidence_blocked() -> None:
    os.environ["PLANNER_PROMOTION_MIN_CONFIDENCE"] = "0.75"
    rs = _runtime_state()
    promo = evaluate_promotion(_dec("read"), _dec("read"), _dec("read", confidence=0.5), rs)
    assert promo.eligible is False
    assert any("blocked_by_confidence" in b for b in promo.blocked_reasons)
    print("PASS test_low_confidence_blocked")


def test_risk_flag_blocked() -> None:
    rs = _runtime_state()
    promo = evaluate_promotion(
        _dec("read"),
        _dec("read"),
        _dec("read", risk_flags=["coverage_gap"]),
        rs,
    )
    assert promo.eligible is False
    assert any("blocked_by_risk" in b for b in promo.blocked_reasons)
    print("PASS test_risk_flag_blocked")


def test_code_edit_intent_blocked() -> None:
    rs = _runtime_state(router_intent="code_edit")
    promo = evaluate_promotion(_dec("read"), _dec("read"), _dec("read"), rs)
    assert promo.eligible is False
    assert any("blocked_by_intent" in b for b in promo.blocked_reasons)
    print("PASS test_code_edit_intent_blocked")


def test_dry_run_tool_call_shape() -> None:
    rs = _runtime_state()
    llm = _dec("grep", targets=["dir.adapters"], tool_args={"pattern": "import |from"})
    promo = evaluate_promotion(_dec("grep", targets=["dir.adapters"]), _dec("grep", targets=["dir.adapters"]), llm, rs)
    dry = promo.dry_run_tool_call
    assert dry.get("shadow_only") is True
    fn = dry.get("function") or {}
    assert fn.get("name") == "GrepSource"
    args = json.loads(fn.get("arguments") or "{}")
    assert args.get("source_id") == "dir.adapters"
    assert "pattern" in args
    print("PASS test_dry_run_tool_call_shape")


def test_hot_path_unchanged(tmp: Path) -> None:
    os.environ["EXPLORER_TRACE_PATH"] = str(tmp / "promo.ndjson")
    os.environ["EXPLORER_TRACE_STDOUT"] = "0"
    os.environ["PLANNER_SHADOW_MODE"] = "1"
    os.environ["LLM_PLANNER_SHADOW_ENABLED"] = "1"
    os.environ["PLANNER_PROMOTION_GATE_ENABLED"] = "1"
    os.environ["PLANNER_PROMOTION_SHADOW_ONLY"] = "1"

    state = SessionState()
    state.current_query = "analyze router"
    state.agent_plan = {
        "next_action": {"tool": "Read", "target": "router/main.py", "reason": "entry"},
        "evidence_needed": [],
        "evidence_collected": [],
    }
    state.project_index = {"entrypoints": ["router/main.py"]}
    state.last_working_set = {"priority_targets": ["router/main.py"]}
    ap_before = json.dumps(state.agent_plan, sort_keys=True)

    with mock.patch(
        "agent_brain.llm_planner._invoke_llm",
        return_value=(
            json.dumps({
                "action": "read",
                "target_files": ["router/main.py"],
                "reason": "promotable",
                "confidence": 0.92,
                "risk_flags": [],
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
    promo = payload.get("promotion_decision") or {}
    assert promo.get("eligible") is True
    assert promo.get("dry_run_tool_call")
    print("PASS test_hot_path_unchanged")


def test_explorer_trace_promotion_events(tmp: Path) -> None:
    trace = tmp / "promo-trace.ndjson"
    os.environ["EXPLORER_TRACE_PATH"] = str(trace)
    os.environ["EXPLORER_TRACE_STDOUT"] = "0"
    os.environ["PLANNER_SHADOW_MODE"] = "1"
    os.environ["LLM_PLANNER_SHADOW_ENABLED"] = "1"
    os.environ["PLANNER_PROMOTION_GATE_ENABLED"] = "1"

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
        run_planner_shadow(state, query="q", phase="tool_planning", router_intent="read_only_analysis")

    events = {json.loads(ln)["event"] for ln in trace.read_text(encoding="utf-8").splitlines() if ln.strip()}
    assert "planner.promotion.evaluated" in events
    assert "planner.promotion.eligible" in events
    print("PASS test_explorer_trace_promotion_events")


def test_metrics_snapshot() -> None:
    reset_promotion_metrics()
    rs = _runtime_state()
    evaluate_promotion(_dec("read"), _dec("read"), _dec("read"), rs)
    evaluate_promotion(_dec("read"), _dec("read"), _dec("edit"), rs)
    snap = promotion_metrics_snapshot()
    assert snap["evaluations"] == 2
    assert snap["eligible"] == 1
    assert snap["blocked_by_action"] >= 1
    assert snap["eligible_rate"] == 0.5
    print("PASS test_metrics_snapshot")


def main() -> int:
    os.environ.setdefault("EXPLORER_TRACE_ENABLED", "1")
    os.environ.setdefault("PLANNER_PROMOTION_MIN_CONFIDENCE", "0.75")
    os.environ.setdefault("PLANNER_PROMOTION_MIN_TARGET_OVERLAP", "0.5")
    reset_promotion_metrics()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_read_eligible()
        test_grep_eligible_via_target_overlap()
        test_glob_eligible()
        test_edit_blocked()
        test_shell_blocked()
        test_final_blocked()
        test_low_confidence_blocked()
        test_risk_flag_blocked()
        test_code_edit_intent_blocked()
        test_dry_run_tool_call_shape()
        test_hot_path_unchanged(tmp)
        test_explorer_trace_promotion_events(tmp)
    test_metrics_snapshot()
    print("\nAll planner promotion gate E2E tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
