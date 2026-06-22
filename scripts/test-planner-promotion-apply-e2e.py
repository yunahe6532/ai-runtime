#!/usr/bin/env python3
"""Phase 2.2b — Planner promotion apply E2E (read/grep/glob only)."""

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
    apply_planner_promotion_if_allowed,
    evaluate_promotion,
    reset_promotion_metrics,
    should_apply_promotion,
)
from agent_brain.runtime_state import RuntimeStateBuilder  # noqa: E402
from legacy.memory_store import SessionState  # noqa: E402


def _env_on() -> None:
    os.environ["EXPLORER_TRACE_ENABLED"] = "1"
    os.environ["PLANNER_PROMOTION_GATE_ENABLED"] = "1"
    os.environ["PLANNER_PROMOTION_SHADOW_ONLY"] = "0"
    os.environ["PLANNER_PROMOTION_ENABLE_READONLY"] = "1"
    os.environ["PLANNER_PROMOTION_MIN_CONFIDENCE"] = "0.75"
    os.environ["PLANNER_PROMOTION_MIN_TARGET_OVERLAP"] = "0.5"
    os.environ["PLANNER_PROMOTION_MAX_PER_TURN"] = "1"
    os.environ["PLANNER_SHADOW_MODE"] = "1"
    os.environ["LLM_PLANNER_SHADOW_ENABLED"] = "1"


def _env_default() -> None:
    os.environ["PLANNER_PROMOTION_GATE_ENABLED"] = "1"
    os.environ["PLANNER_PROMOTION_SHADOW_ONLY"] = "1"
    os.environ["PLANNER_PROMOTION_ENABLE_READONLY"] = "0"


def _state(*, router_intent: str = "read_only_analysis") -> SessionState:
    state = SessionState()
    state.current_query = "analyze router structure"
    state.agent_plan = {
        "next_action": {"tool": "Read", "target": "router/main.py", "reason": "rule entry"},
        "router_intent": router_intent,
        "evidence_needed": ["core_files_seen"],
        "evidence_collected": [],
        "exploration_actions_tried": [],
    }
    state.project_index = {"file_count": 5, "entrypoints": ["router/main.py"]}
    state.last_working_set = {"priority_targets": ["router/main.py"], "must_include": []}
    return state


def _llm_payload(action: str, *, targets: list[str] | None = None, **extra) -> str:
    body = {
        "action": action,
        "target_files": targets or ["router/main.py"],
        "reason": f"llm {action}",
        "confidence": 0.9,
        "risk_flags": [],
        **extra,
    }
    return json.dumps(body)


def _run_shadow_and_apply(
    state: SessionState,
    llm_json: str,
    *,
    router_intent: str = "read_only_analysis",
) -> dict:
    with mock.patch(
        "agent_brain.llm_planner._invoke_llm",
        return_value=(llm_json, {"status": "ok"}),
    ):
        run_planner_shadow(
            state,
            query=state.current_query,
            phase="tool_planning",
            router_intent=router_intent,
            coverage=SimpleNamespace(to_dict=lambda: {"complete": False, "coverage_score": 0.3}),
        )
    return apply_planner_promotion_if_allowed(state, phase="tool_planning")


def test_read_applied() -> None:
    _env_on()
    state = _state()
    before = dict(state.agent_plan["next_action"])
    result = _run_shadow_and_apply(state, _llm_payload("read"))
    assert result["applied"] is True
    na = state.agent_plan["next_action"]
    assert na["tool"] == "ReadSource"
    assert na.get("source") == "llm_planner_promotion"
    assert na.get("shadow_only") is False
    assert state.agent_plan.get("original_rule_action") == before
    print("PASS test_read_applied")


def test_grep_applied() -> None:
    _env_on()
    state = _state()
    result = _run_shadow_and_apply(
        state,
        _llm_payload("grep", targets=["router/main.py"], target_symbols=["class |def"]),
    )
    assert result["applied"] is True
    assert state.agent_plan["next_action"]["tool"] == "GrepSource"
    print("PASS test_grep_applied")


def test_glob_applied() -> None:
    _env_on()
    state = _state()
    state.agent_plan["next_action"] = {
        "tool": "Glob",
        "target": "dir.runtime_core",
        "reason": "rule",
    }
    result = _run_shadow_and_apply(
        state,
        _llm_payload("glob", targets=["dir.runtime_core"], glob_pattern="*.py"),
    )
    assert result["applied"] is True
    assert state.agent_plan["next_action"]["tool"] == "GlobSource"
    print("PASS test_glob_applied")


def test_shadow_only_not_applied() -> None:
    _env_default()
    os.environ["PLANNER_PROMOTION_SHADOW_ONLY"] = "1"
    state = _state()
    before = json.dumps(state.agent_plan, sort_keys=True)
    result = _run_shadow_and_apply(state, _llm_payload("read"))
    assert result["applied"] is False
    assert json.dumps(state.agent_plan, sort_keys=True) == before
    print("PASS test_shadow_only_not_applied")


def test_enable_readonly_off_not_applied() -> None:
    os.environ["PLANNER_PROMOTION_SHADOW_ONLY"] = "0"
    os.environ["PLANNER_PROMOTION_ENABLE_READONLY"] = "0"
    state = _state()
    before = json.dumps(state.agent_plan, sort_keys=True)
    result = _run_shadow_and_apply(state, _llm_payload("read"))
    assert result["applied"] is False
    assert json.dumps(state.agent_plan, sort_keys=True) == before
    print("PASS test_enable_readonly_off_not_applied")


def test_code_edit_intent_blocked() -> None:
    _env_on()
    state = _state(router_intent="code_edit")
    result = _run_shadow_and_apply(state, _llm_payload("read"), router_intent="code_edit")
    assert result["applied"] is False
    promo = state.last_planner_promotion or {}
    assert promo.get("eligible") is False
    print("PASS test_code_edit_intent_blocked")


def test_edit_shell_final_blocked() -> None:
    _env_on()
    state = _state()
    for action in ("edit", "shell", "final"):
        st = _state()
        result = _run_shadow_and_apply(st, _llm_payload(action))
        assert result["applied"] is False
        assert (st.last_planner_promotion or {}).get("eligible") is False
    print("PASS test_edit_shell_final_blocked")


def test_vendor_target_blocked() -> None:
    _env_on()
    state = _state()
    result = _run_shadow_and_apply(
        state,
        _llm_payload("read", targets=["node_modules/foo/index.js"]),
    )
    assert result["applied"] is False
    promo = state.last_planner_promotion or {}
    assert promo.get("eligible") is False
    print("PASS test_vendor_target_blocked")


def test_repeated_same_tool_blocked() -> None:
    _env_on()
    state = _state()
    state.agent_plan["exploration_actions_tried"] = ["read:router/main.py"]
    result = _run_shadow_and_apply(state, _llm_payload("read", targets=["router/main.py"]))
    assert result["applied"] is False
    assert "repeat" in (result.get("reason") or "")
    print("PASS test_repeated_same_tool_blocked")


def test_trace_apply_events(tmp: Path) -> None:
    _env_on()
    trace = tmp / "apply-trace.ndjson"
    os.environ["EXPLORER_TRACE_PATH"] = str(trace)
    os.environ["EXPLORER_TRACE_STDOUT"] = "0"
    state = _state()
    _run_shadow_and_apply(state, _llm_payload("read"))
    events = {json.loads(ln)["event"] for ln in trace.read_text(encoding="utf-8").splitlines() if ln.strip()}
    assert "planner.promotion.applied" in events
    print("PASS test_trace_apply_events")


def test_should_apply_unit() -> None:
    _env_on()
    state = _state()
    rs = RuntimeStateBuilder().build(
        session_state=state,
        query=state.current_query,
        phase="tool_planning",
        router_intent="read_only_analysis",
        coverage=SimpleNamespace(to_dict=lambda: {"complete": False}),
    )
    rule = PlannerDecision(action="read", target_files=["router/main.py"], confidence=0.8)
    llm = PlannerDecision(action="read", target_files=["router/main.py"], confidence=0.9)
    promo = evaluate_promotion(rule, rule, llm, rs, session_state=state)
    ok, reason = should_apply_promotion(promo, rs, llm=llm, session_state=state)
    assert ok is True
    assert reason == "apply_ok"
    print("PASS test_should_apply_unit")


def main() -> int:
    os.environ.setdefault("EXPLORER_TRACE_ENABLED", "1")
    reset_promotion_metrics()
    test_read_applied()
    test_grep_applied()
    test_glob_applied()
    test_shadow_only_not_applied()
    test_enable_readonly_off_not_applied()
    test_code_edit_intent_blocked()
    test_edit_shell_final_blocked()
    test_vendor_target_blocked()
    test_repeated_same_tool_blocked()
    test_should_apply_unit()
    with tempfile.TemporaryDirectory() as td:
        test_trace_apply_events(Path(td))
    print("\nAll planner promotion apply E2E tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
