#!/usr/bin/env python3
"""Live promotion validation harness — multi-intent scenarios with promotion env on."""

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

from agent_brain.planner_shadow import run_planner_shadow  # noqa: E402
from agent_brain.promotion_gate import (  # noqa: E402
    apply_planner_promotion_if_allowed,
    promotion_metrics_snapshot,
    reset_planner_promotion_turn,
    reset_promotion_metrics,
)
from legacy.memory_store import SessionState  # noqa: E402


def _promotion_env_on() -> None:
    os.environ["EXPLORER_TRACE_ENABLED"] = "1"
    os.environ["EXPLORER_TRACE_STDOUT"] = "0"
    os.environ["PLANNER_SHADOW_MODE"] = "1"
    os.environ["LLM_PLANNER_SHADOW_ENABLED"] = "1"
    os.environ["PLANNER_PROMOTION_GATE_ENABLED"] = "1"
    os.environ["PLANNER_PROMOTION_SHADOW_ONLY"] = "0"
    os.environ["PLANNER_PROMOTION_ENABLE_READONLY"] = "1"
    os.environ["PLANNER_PROMOTION_MAX_PER_TURN"] = "1"
    os.environ["PLANNER_PROMOTION_MIN_CONFIDENCE"] = "0.75"
    os.environ["PLANNER_PROMOTION_MIN_TARGET_OVERLAP"] = "0.5"


def _scenario(
    name: str,
    *,
    intent: str,
    llm_action: str,
    targets: list[str] | None = None,
    extra: dict | None = None,
    rule_tool: str = "Read",
    rule_target: str = "router/main.py",
) -> dict:
    return {
        "name": name,
        "intent": intent,
        "llm_action": llm_action,
        "targets": targets or ["router/main.py"],
        "extra": extra or {},
        "rule_tool": rule_tool,
        "rule_target": rule_target,
    }


SCENARIOS = [
    _scenario("read_only_read", intent="read_only_analysis", llm_action="read"),
    _scenario("architecture_grep", intent="architecture", llm_action="grep", extra={"target_symbols": ["class |def"]}),
    _scenario("exploration_glob", intent="exploration", llm_action="glob", targets=["dir.runtime_core"], rule_tool="Glob", rule_target="dir.runtime_core", extra={"glob_pattern": "*.py"}),
    _scenario("project_inspection_read", intent="project_inspection", llm_action="read"),
    _scenario("doc_analysis_read", intent="doc_analysis", llm_action="read", targets=["docs/VISION.md"]),
    _scenario("code_edit_blocked", intent="code_edit", llm_action="read"),
    _scenario("edit_action_blocked", intent="read_only_analysis", llm_action="edit"),
    _scenario("shell_action_blocked", intent="read_only_analysis", llm_action="shell"),
    _scenario("final_action_blocked", intent="read_only_analysis", llm_action="final"),
    _scenario("vendor_target_blocked", intent="read_only_analysis", llm_action="read", targets=["node_modules/foo/index.js"]),
    _scenario("low_confidence", intent="read_only_analysis", llm_action="read", extra={"confidence": 0.4}),
]


def _run_one(sc: dict, trace_path: Path) -> dict:
    os.environ["EXPLORER_TRACE_PATH"] = str(trace_path)
    state = SessionState()
    state.current_query = f"scenario {sc['name']}"
    state.agent_plan = {
        "next_action": {"tool": sc["rule_tool"], "target": sc["rule_target"], "reason": "rule"},
        "router_intent": sc["intent"],
        "evidence_needed": ["core_files_seen"],
        "evidence_collected": [],
        "exploration_actions_tried": list(sc.get("tried") or []),
    }
    state.project_index = {"entrypoints": ["router/main.py"], "file_count": 10}
    state.last_working_set = {"priority_targets": [sc["rule_target"]], "must_include": []}

    body = {
        "action": sc["llm_action"],
        "target_files": sc["targets"],
        "reason": f"llm {sc['llm_action']}",
        "confidence": sc.get("extra", {}).get("confidence", 0.9),
        "risk_flags": [],
        **{k: v for k, v in sc.get("extra", {}).items() if k != "confidence"},
    }
    reset_planner_promotion_turn(state)
    with mock.patch(
        "agent_brain.llm_planner._invoke_llm",
        return_value=(json.dumps(body), {"status": "ok"}),
    ):
        run_planner_shadow(
            state,
            query=state.current_query,
            phase="tool_planning",
            router_intent=sc["intent"],
            coverage=SimpleNamespace(to_dict=lambda: {"complete": False, "coverage_score": 0.3}),
        )
    apply_result = apply_planner_promotion_if_allowed(state, phase="tool_planning")
    promo = dict(state.last_planner_promotion or {})
    na = dict((state.agent_plan or {}).get("next_action") or {})
    return {
        "scenario": sc["name"],
        "intent": sc["intent"],
        "llm_action": sc["llm_action"],
        "eligible": promo.get("eligible"),
        "applied": apply_result.get("applied"),
        "blocked": apply_result.get("blocked"),
        "skipped": apply_result.get("skipped"),
        "reason": apply_result.get("reason") or promo.get("reason"),
        "next_tool": na.get("tool"),
        "promotion_source": na.get("source"),
    }


def main() -> int:
    _promotion_env_on()
    reset_promotion_metrics()

    out_json = ROOT / "tmp" / "planner-promotion-live-validation.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        trace = Path(td) / "promotion-live.ndjson"
        results = [_run_one(sc, trace) for sc in SCENARIOS]

        # run metrics audit on trace
        import subprocess

        audit = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "audit-planner-promotion-metrics.py"), "--trace", str(trace)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )

    snap = promotion_metrics_snapshot()
    payload = {
        "scenarios": results,
        "in_process_metrics": snap,
        "audit_stdout": audit.stdout,
        "audit_stderr": audit.stderr,
        "audit_rc": audit.returncode,
    }
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    applied = sum(1 for r in results if r.get("applied"))
    eligible = sum(1 for r in results if r.get("eligible"))
    blocked = sum(1 for r in results if r.get("blocked"))
    skipped = sum(1 for r in results if r.get("skipped"))

    print("=== Promotion Live Harness ===")
    print(f"scenarios: {len(results)}")
    print(f"eligible: {eligible}  applied: {applied}  blocked: {blocked}  skipped: {skipped}")
    print(f"in_process: {json.dumps(snap)}")
    print(f"\nWritten: {out_json}")
    for r in results:
        mark = "APPLIED" if r.get("applied") else ("SKIP" if r.get("skipped") else "BLOCK")
        print(f"  [{mark}] {r['scenario']:28} intent={r['intent']:22} tool={r.get('next_tool')}")
    return 0 if audit.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
