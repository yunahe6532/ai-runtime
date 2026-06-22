"""Agent Brain — Planner promotion gate (Phase 2.2a: evaluate only, no hot path change)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from .planner_contract import PlannerDecision
from .planner_shadow import compare_shadow_decisions
from .runtime_state import RuntimeState

LOG = logging.getLogger("agent_brain.promotion_gate")

PROMOTABLE_ACTIONS = frozenset({"read", "grep", "glob"})
BLOCKED_HOT_ACTIONS = frozenset({"shell", "edit", "final", "summarize", "ask_user", "recover"})

PROMOTION_ALLOWED_INTENTS = frozenset({
    "read_only_analysis",
    "read_only_exploration",
    "analysis",
    "exploration",
    "architecture",
})

PROMOTION_BLOCKED_INTENTS = frozenset({
    "code_edit",
    "benchmark",
    "shell_task",
    "agent",
    "debug",
    "bugfix",
    "log_analysis",
})

_metrics: dict[str, int] = {
    "evaluations": 0,
    "eligible": 0,
    "blocked_by_action": 0,
    "blocked_by_intent": 0,
    "blocked_by_confidence": 0,
    "blocked_by_risk": 0,
    "blocked_by_alignment": 0,
    "would_change_hot_path": 0,
}


def promotion_shadow_only() -> bool:
    return os.getenv("PLANNER_PROMOTION_SHADOW_ONLY", "1") != "0"


def promotion_gate_enabled() -> bool:
    return os.getenv("PLANNER_PROMOTION_GATE_ENABLED", "1") != "0"


def promotion_min_confidence() -> float:
    try:
        return float(os.getenv("PLANNER_PROMOTION_MIN_CONFIDENCE", "0.75"))
    except ValueError:
        return 0.75


def promotion_min_target_overlap() -> float:
    try:
        return float(os.getenv("PLANNER_PROMOTION_MIN_TARGET_OVERLAP", "0.5"))
    except ValueError:
        return 0.5


@dataclass
class PromotionDecision:
    eligible: bool = False
    allowed_action: str = "none"
    reason: str = ""
    blocked_reasons: list[str] = field(default_factory=list)
    confidence: float = 0.0
    target_overlap: float = 0.0
    evidence_overlap: float = 0.0
    risk_flags: list[str] = field(default_factory=list)
    would_change_hot_path: bool = False
    dry_run_tool_call: dict[str, Any] = field(default_factory=dict)
    shadow_only: bool = True
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _overlap(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _targets(decision: PlannerDecision) -> set[str]:
    return {t.lower() for t in decision.target_files if t}


def _is_promotion_intent(runtime_state: RuntimeState) -> bool:
    ri = (runtime_state.router_intent or "").lower().strip()
    ci = (runtime_state.context_intent or "").lower().strip()
    if ri in PROMOTION_BLOCKED_INTENTS or ci in PROMOTION_BLOCKED_INTENTS:
        return False
    if ri in PROMOTION_ALLOWED_INTENTS or ci in PROMOTION_ALLOWED_INTENTS:
        return True
    for token in (ri, ci):
        if not token:
            continue
        if "read_only" in token or "analysis" in token or "exploration" in token:
            return True
    task = str((runtime_state.raw or {}).get("agent_plan_excerpt", {}).get("goal") or "").lower()
    if "code_edit" in task or "수정" in task or "fix bug" in task:
        return False
    return False


def _build_dry_run_tool_call(llm: PlannerDecision) -> dict[str, Any]:
    action = llm.action
    if action == "read":
        target = llm.target_files[0] if llm.target_files else ""
        source_id = llm.tool_args.get("source_id") or target or "file.unknown"
        return {
            "type": "function",
            "function": {
                "name": "ReadSource",
                "arguments": json.dumps({"source_id": source_id}, ensure_ascii=False),
            },
            "shadow_only": True,
        }
    if action == "grep":
        pattern = (
            llm.tool_args.get("pattern")
            or (llm.target_symbols[0] if llm.target_symbols else "class |def")
        )
        source_id = llm.tool_args.get("source_id") or (llm.target_files[0] if llm.target_files else "dir.unknown")
        return {
            "type": "function",
            "function": {
                "name": "GrepSource",
                "arguments": json.dumps(
                    {"source_id": source_id, "pattern": pattern},
                    ensure_ascii=False,
                ),
            },
            "shadow_only": True,
        }
    if action == "glob":
        glob_pat = llm.tool_args.get("glob_pattern") or "*.py"
        source_id = llm.tool_args.get("source_id") or (llm.target_files[0] if llm.target_files else "dir.unknown")
        return {
            "type": "function",
            "function": {
                "name": "GlobSource",
                "arguments": json.dumps(
                    {"source_id": source_id, "glob_pattern": glob_pat},
                    ensure_ascii=False,
                ),
            },
            "shadow_only": True,
        }
    return {}


def _record_metrics(decision: PromotionDecision) -> None:
    _metrics["evaluations"] += 1
    if decision.eligible:
        _metrics["eligible"] += 1
    for reason in decision.blocked_reasons:
        if reason.startswith("blocked_by_action"):
            _metrics["blocked_by_action"] += 1
        elif reason.startswith("blocked_by_intent"):
            _metrics["blocked_by_intent"] += 1
        elif reason.startswith("blocked_by_confidence"):
            _metrics["blocked_by_confidence"] += 1
        elif reason.startswith("blocked_by_risk"):
            _metrics["blocked_by_risk"] += 1
        elif reason.startswith("blocked_by_alignment"):
            _metrics["blocked_by_alignment"] += 1
    if decision.would_change_hot_path:
        _metrics["would_change_hot_path"] += 1


def promotion_metrics_snapshot() -> dict[str, Any]:
    n = max(_metrics["evaluations"], 1)
    return {
        **dict(_metrics),
        "eligible_rate": round(_metrics["eligible"] / n, 4),
        "would_change_hot_path_rate": round(_metrics["would_change_hot_path"] / n, 4),
    }


def reset_promotion_metrics() -> None:
    for k in _metrics:
        _metrics[k] = 0


def evaluate_promotion(
    rule: PlannerDecision,
    heuristic: PlannerDecision,
    llm: PlannerDecision,
    runtime_state: RuntimeState,
    *,
    phase: str = "",
) -> PromotionDecision:
    """Decide whether LLM planner read/grep/glob could be promoted (shadow-only by default)."""
    blocked: list[str] = []
    min_conf = promotion_min_confidence()
    min_overlap = promotion_min_target_overlap()

    rule_llm = compare_shadow_decisions(rule, llm, phase=phase, label_a="rule", label_b="llm")
    target_overlap = float(rule_llm.get("target_overlap") or 0.0)
    evidence_overlap = float(rule_llm.get("evidence_overlap") or 0.0)
    would_change = bool(rule_llm.get("would_change_hot_path"))

    if llm.action in BLOCKED_HOT_ACTIONS:
        blocked.append(f"blocked_by_action:{llm.action}")
    if llm.action not in PROMOTABLE_ACTIONS:
        blocked.append(f"blocked_by_action:not_promotable:{llm.action}")

    if not _is_promotion_intent(runtime_state):
        blocked.append("blocked_by_intent:not_read_only_analysis")

    if llm.risk_flags:
        blocked.append(f"blocked_by_risk:{','.join(llm.risk_flags[:4])}")

    if float(llm.confidence) < min_conf:
        blocked.append(f"blocked_by_confidence:{llm.confidence}<{min_conf}")

    action_aligned = (
        rule.action == llm.action
        or heuristic.action == llm.action
        or (rule.action == heuristic.action == llm.action)
    )
    heuristic_llm_overlap = _overlap(_targets(heuristic), _targets(llm))
    overlap_ok = target_overlap >= min_overlap or heuristic_llm_overlap >= min_overlap
    if not action_aligned and not overlap_ok:
        blocked.append(
            f"blocked_by_alignment:action_match={action_aligned} "
            f"target_overlap={target_overlap:.3f} heuristic_overlap={heuristic_llm_overlap:.3f}"
        )

    eligible = len(blocked) == 0 and llm.action in PROMOTABLE_ACTIONS
    allowed_action = llm.action if eligible else "none"

    if eligible:
        reason = f"promotion eligible: llm {llm.action} aligned (conf={llm.confidence:.2f})"
    else:
        reason = f"promotion blocked: {blocked[0] if blocked else 'unknown'}"

    dry_run = _build_dry_run_tool_call(llm) if llm.action in PROMOTABLE_ACTIONS else {}

    decision = PromotionDecision(
        eligible=eligible,
        allowed_action=allowed_action,
        reason=reason,
        blocked_reasons=blocked,
        confidence=float(llm.confidence),
        target_overlap=round(target_overlap, 3),
        evidence_overlap=round(evidence_overlap, 3),
        risk_flags=list(llm.risk_flags),
        would_change_hot_path=would_change,
        dry_run_tool_call=dry_run,
        shadow_only=promotion_shadow_only(),
        metrics=promotion_metrics_snapshot(),
    )
    _record_metrics(decision)
    decision.metrics = promotion_metrics_snapshot()
    return decision
