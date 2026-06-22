"""Agent Brain — Planner promotion gate (Phase 2.2a evaluate, 2.2b read-only apply)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from runtime_kernel.project_index import PathClass, classify_path, path_included_in_index

from .planner_contract import PlannerDecision
from .planner_shadow import compare_shadow_decisions
from .runtime_state import RuntimeState

LOG = logging.getLogger("agent_brain.promotion_gate")

PROMOTABLE_ACTIONS = frozenset({"read", "grep", "glob"})
BLOCKED_HOT_ACTIONS = frozenset({"shell", "edit", "final", "summarize", "ask_user", "recover"})

PROMOTION_ALLOWED_INTENTS = frozenset({
    "read_only_analysis",
    "architecture",
    "project_inspection",
    "exploration",
    "doc_analysis",
    "read_only_exploration",
    "analysis",
})

PROMOTION_BLOCKED_INTENTS = frozenset({
    "code_edit",
    "exec",
    "shell",
    "deployment",
    "security_sensitive",
    "unknown",
    "benchmark",
    "shell_task",
    "agent",
    "debug",
    "bugfix",
    "log_analysis",
})

BLOCKED_TARGET_CLASSES = frozenset({
    PathClass.VENDOR,
    PathClass.GENERATED,
    PathClass.RUNTIME_DATA,
    PathClass.CACHE,
    PathClass.GIT_METADATA,
})

ACTION_TOOL_NAMES = {
    "read": "ReadSource",
    "grep": "GrepSource",
    "glob": "GlobSource",
}

FINAL_PHASES = frozenset({"final_answer", "partial_final_answer", "recovery_final"})

_metrics: dict[str, int] = {
    "evaluations": 0,
    "eligible": 0,
    "applied": 0,
    "skipped": 0,
    "apply_blocked": 0,
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


def promotion_enable_readonly() -> bool:
    return os.getenv("PLANNER_PROMOTION_ENABLE_READONLY", "0") == "1"


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


def promotion_max_per_turn() -> int:
    try:
        return max(0, int(os.getenv("PLANNER_PROMOTION_MAX_PER_TURN", "1")))
    except ValueError:
        return 1


def _env_flag(env: dict[str, str] | None, key: str, default: str) -> bool:
    if env is not None and key in env:
        return str(env[key]).strip() not in ("0", "false", "False", "")
    return os.getenv(key, default) not in ("0", "false", "False", "")


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
    apply_allowed: bool = False
    apply_reason: str = ""
    effective_action: str = ""
    effective_tool_call: dict[str, Any] = field(default_factory=dict)
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
    for token in (ri, ci):
        if token in PROMOTION_BLOCKED_INTENTS:
            return False
    for token in (ri, ci):
        if token in PROMOTION_ALLOWED_INTENTS:
            return True
    for token in (ri, ci):
        if not token:
            continue
        if "read_only" in token or token in ("exploration", "architecture", "inspection"):
            return True
    task = str((runtime_state.raw or {}).get("agent_plan_excerpt", {}).get("goal") or "").lower()
    if any(x in task for x in ("code_edit", "수정", "fix bug", "deploy", "shell")):
        return False
    return ri in PROMOTION_ALLOWED_INTENTS or ci in PROMOTION_ALLOWED_INTENTS


def _collect_target_paths(llm: PlannerDecision) -> list[str]:
    paths: list[str] = []
    for t in llm.target_files:
        if t:
            paths.append(str(t))
    for key in ("path", "source_id", "target"):
        val = llm.tool_args.get(key)
        if val:
            paths.append(str(val))
    return paths


def _validate_promotion_target(llm: PlannerDecision) -> tuple[bool, str]:
    paths = _collect_target_paths(llm)
    if not paths:
        if llm.action in PROMOTABLE_ACTIONS:
            return False, "promotion blocked: invalid target"
        return True, ""
    for raw in paths:
        p = raw.replace("\\", "/").lstrip("/")
        if not p:
            continue
        lower = p.lower()
        if any(x in lower for x in ("node_modules", "/vendor/", "/archive/", "/tmp/", "runtime_data")):
            return False, "promotion blocked: invalid target"
        if "." in p and "/" in p or p.endswith((".py", ".md", ".yaml", ".yml", ".json", ".sh")):
            pc = classify_path(p)
            if pc in BLOCKED_TARGET_CLASSES:
                return False, f"promotion blocked: invalid target ({pc.value})"
            if not path_included_in_index(p):
                return False, "promotion blocked: invalid target"
        elif p.startswith(("dir.", "file.")):
            continue
        elif llm.action == "read" and not path_included_in_index(p):
            return False, "promotion blocked: invalid target"
    return True, ""


def _resolve_source_id(llm: PlannerDecision) -> str:
    args = llm.tool_args or {}
    if args.get("source_id"):
        return str(args["source_id"])
    if llm.target_files:
        return str(llm.target_files[0])
    return str(args.get("path") or args.get("target") or "")


def _build_dry_run_tool_call(llm: PlannerDecision) -> dict[str, Any]:
    return _build_effective_tool_call(llm, shadow_only=True)


def _build_effective_tool_call(
    llm: PlannerDecision,
    *,
    shadow_only: bool = False,
    original_rule_action: dict[str, Any] | None = None,
    promotion_reason: str = "",
) -> dict[str, Any]:
    action = llm.action
    if action not in PROMOTABLE_ACTIONS:
        return {}
    source_id = _resolve_source_id(llm)
    tool_name = ACTION_TOOL_NAMES[action]
    if action == "read":
        args = {"source_id": source_id}
    elif action == "grep":
        pattern = (
            llm.tool_args.get("pattern")
            or (llm.target_symbols[0] if llm.target_symbols else "class |def")
        )
        args = {"source_id": source_id, "pattern": str(pattern)}
    else:
        glob_pat = str(llm.tool_args.get("glob_pattern") or "*.py")
        args = {"source_id": source_id, "glob_pattern": glob_pat}
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
        "source": "llm_planner_promotion",
        "shadow_only": shadow_only,
        "original_rule_action": dict(original_rule_action or {}),
        "promotion_reason": promotion_reason or llm.reason,
        "confidence": float(llm.confidence),
    }


def promotion_to_next_action(
    llm: PlannerDecision,
    *,
    promotion_reason: str = "",
) -> dict[str, Any]:
    """Map LLM planner action to agent_plan next_action (ReadSource/GrepSource/GlobSource)."""
    source_id = _resolve_source_id(llm)
    tool = ACTION_TOOL_NAMES.get(llm.action, "")
    if not tool:
        return {}
    na: dict[str, Any] = {
        "tool": tool,
        "source_id": source_id,
        "target": source_id,
        "reason": promotion_reason or llm.reason or f"llm_planner_promotion:{llm.action}",
        "source": "llm_planner_promotion",
        "shadow_only": False,
        "confidence": float(llm.confidence),
    }
    if llm.action == "grep":
        pattern = (
            llm.tool_args.get("pattern")
            or (llm.target_symbols[0] if llm.target_symbols else "class |def")
        )
        na["pattern"] = str(pattern)
    elif llm.action == "glob":
        na["glob_pattern"] = str(llm.tool_args.get("glob_pattern") or "*.py")
    path = llm.tool_args.get("path") or (llm.target_files[0] if llm.target_files else "")
    if path and "/" in str(path):
        na["path"] = str(path)
    return na


def build_effective_planner_action(
    llm: PlannerDecision,
    rule: PlannerDecision,
    promotion: PromotionDecision,
) -> PlannerDecision:
    """Structured effective action — does not mutate agent_plan."""
    tool_name = ACTION_TOOL_NAMES.get(llm.action, "")
    return PlannerDecision(
        action=llm.action,
        target_files=list(llm.target_files),
        target_symbols=list(llm.target_symbols),
        reason=promotion.reason or llm.reason,
        evidence_needed=list(llm.evidence_needed),
        confidence=float(llm.confidence),
        risk_flags=list(llm.risk_flags),
        tool_name=tool_name,
        tool_args=dict(llm.tool_args or {}),
    )


def should_apply_promotion(
    promotion: PromotionDecision,
    runtime_state: RuntimeState,
    *,
    llm: PlannerDecision | None = None,
    session_state: Any = None,
    phase: str = "",
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Kill-switch aware apply gate — read/grep/glob only."""
    if not promotion.eligible:
        return False, "not_eligible"
    if not _env_flag(env, "PLANNER_PROMOTION_GATE_ENABLED", "1"):
        return False, "gate_disabled"
    if _env_flag(env, "PLANNER_PROMOTION_SHADOW_ONLY", "1"):
        return False, "shadow_only"
    if not _env_flag(env, "PLANNER_PROMOTION_ENABLE_READONLY", "0"):
        return False, "readonly_disabled"
    if phase in FINAL_PHASES or (runtime_state.phase or "") in FINAL_PHASES:
        return False, "blocked_by_phase"
    if llm is None or llm.action not in PROMOTABLE_ACTIONS:
        return False, "no_llm_action"
    if llm.risk_flags:
        return False, f"blocked_by_risk:{','.join(llm.risk_flags[:4])}"
    if float(llm.confidence) < promotion_min_confidence():
        return False, "blocked_by_confidence"

    ok_target, target_reason = _validate_promotion_target(llm)
    if not ok_target:
        return False, target_reason

    max_per = promotion_max_per_turn()
    applied = int(getattr(session_state, "planner_promotion_applied_count", 0) or 0)
    if max_per >= 0 and applied >= max_per:
        return False, "blocked_by_per_turn_limit"

    if session_state is not None:
        ap = dict(getattr(session_state, "agent_plan", None) or {})
        try:
            from reference.read_only_explorer import actions_tried_set, exploration_action_sig

            na = promotion_to_next_action(llm)
            tool = str(na.get("tool") or "")
            sid = str(na.get("source_id") or "")
            if tool and sid:
                sig = exploration_action_sig(
                    tool,
                    sid,
                    pattern=str(na.get("pattern") or ""),
                    glob_pattern=str(na.get("glob_pattern") or ""),
                )
                if sig in actions_tried_set(ap):
                    return False, "blocked_by_repeat"
        except ImportError:
            pass

    return True, "apply_ok"


def reset_planner_promotion_turn(session_state: Any) -> None:
    if session_state is None:
        return
    session_state.planner_promotion_applied_count = 0


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
        "applied_rate": round(_metrics["applied"] / max(_metrics["evaluations"], 1), 4),
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
    session_state: Any = None,
) -> PromotionDecision:
    """Decide whether LLM planner read/grep/glob could be promoted."""
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

    ok_target, target_reason = _validate_promotion_target(llm)
    if not ok_target:
        blocked.append(target_reason)

    eligible = len(blocked) == 0 and llm.action in PROMOTABLE_ACTIONS
    allowed_action = llm.action if eligible else "none"

    if eligible:
        reason = f"promotion eligible: llm {llm.action} aligned (conf={llm.confidence:.2f})"
    else:
        reason = f"promotion blocked: {blocked[0] if blocked else 'unknown'}"

    dry_run = _build_dry_run_tool_call(llm) if llm.action in PROMOTABLE_ACTIONS else {}
    original_na = dict((runtime_state.raw or {}).get("agent_plan_excerpt", {}).get("next_action") or {})

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
    )

    apply_ok, apply_reason = should_apply_promotion(
        decision,
        runtime_state,
        llm=llm,
        session_state=session_state,
        phase=phase or runtime_state.phase or "",
    )
    decision.apply_allowed = apply_ok
    decision.apply_reason = apply_reason
    if apply_ok:
        decision.effective_action = llm.action
        decision.effective_tool_call = _build_effective_tool_call(
            llm,
            shadow_only=False,
            original_rule_action=original_na,
            promotion_reason=reason,
        )

    decision.metrics = promotion_metrics_snapshot()
    _record_metrics(decision)
    decision.metrics = promotion_metrics_snapshot()
    return decision


def apply_planner_promotion_if_allowed(
    session_state: Any,
    *,
    phase: str = "",
) -> dict[str, Any]:
    """Patch agent_plan.next_action when promotion apply gate passes (read/grep/glob only)."""
    result: dict[str, Any] = {
        "applied": False,
        "skipped": False,
        "blocked": False,
        "reason": "",
        "effective_action": "",
        "original_rule_action": {},
        "promotion_source": "llm_planner_promotion",
    }
    if session_state is None:
        result["reason"] = "no_session"
        return result

    promo_dict = dict(getattr(session_state, "last_planner_promotion", None) or {})
    shadow = dict(getattr(session_state, "last_planner_shadow", None) or {})
    llm_dict = shadow.get("llm_shadow_decision") or dict(
        getattr(session_state, "last_planner_llm_shadow", None) or {}
    ).get("decision")
    if not promo_dict:
        result["skipped"] = True
        result["reason"] = "no_promotion_decision"
        _metrics["skipped"] += 1
        _emit_apply_trace(session_state, phase, result)
        return result

    llm = PlannerDecision.from_dict(llm_dict) if llm_dict else None
    runtime_raw = dict(getattr(session_state, "planner_runtime_state", None) or {})
    runtime_state = RuntimeState.from_dict(runtime_raw) if hasattr(RuntimeState, "from_dict") else None
    if runtime_state is None:
        from .runtime_state import RuntimeStateBuilder

        runtime_state = RuntimeStateBuilder().build(
            session_state=session_state,
            query=str(getattr(session_state, "current_query", "") or ""),
            phase=phase,
        )

    promo = PromotionDecision(**{k: promo_dict[k] for k in promo_dict if k in PromotionDecision.__dataclass_fields__})

    apply_ok, apply_reason = should_apply_promotion(
        promo,
        runtime_state,
        llm=llm,
        session_state=session_state,
        phase=phase,
    )
    if not apply_ok:
        result["skipped"] = promo.eligible and apply_reason in ("shadow_only", "readonly_disabled")
        result["blocked"] = not result["skipped"]
        result["reason"] = apply_reason
        if result["blocked"]:
            _metrics["apply_blocked"] += 1
        else:
            _metrics["skipped"] += 1
        _emit_apply_trace(session_state, phase, result, promo=promo_dict)
        return result

    if llm is None:
        result["reason"] = "no_llm_decision"
        result["blocked"] = True
        _metrics["apply_blocked"] += 1
        _emit_apply_trace(session_state, phase, result, promo=promo_dict)
        return result

    ap = dict(getattr(session_state, "agent_plan", None) or {})
    original_na = dict(ap.get("next_action") or {})
    new_na = promotion_to_next_action(llm, promotion_reason=promo.reason)
    if not new_na:
        result["reason"] = "promotion blocked: invalid target"
        result["blocked"] = True
        _metrics["apply_blocked"] += 1
        _emit_apply_trace(session_state, phase, result, promo=promo_dict)
        return result

    new_na["original_rule_action"] = original_na
    new_na["promotion_source"] = "llm_planner_promotion"
    ap["original_rule_action"] = original_na
    ap["next_action"] = new_na
    session_state.agent_plan = ap
    session_state.planner_promotion_applied_count = int(
        getattr(session_state, "planner_promotion_applied_count", 0) or 0
    ) + 1

    result.update({
        "applied": True,
        "reason": apply_reason,
        "effective_action": llm.action,
        "original_rule_action": original_na,
        "effective_next_action": new_na,
        "confidence": float(llm.confidence),
        "target": new_na.get("source_id") or new_na.get("target"),
    })
    _metrics["applied"] += 1

    rt = dict(getattr(session_state, "last_runtime_turn", None) or {})
    rt["planner_promotion_applied"] = True
    rt["effective_action"] = llm.action
    rt["original_rule_action"] = original_na
    rt["promotion_source"] = "llm_planner_promotion"
    session_state.last_runtime_turn = rt

    promo_dict = dict(promo_dict)
    promo_dict["apply_allowed"] = True
    promo_dict["apply_reason"] = apply_reason
    promo_dict["effective_action"] = llm.action
    promo_dict["effective_tool_call"] = _build_effective_tool_call(
        llm,
        shadow_only=False,
        original_rule_action=original_na,
        promotion_reason=promo.reason,
    )
    session_state.last_planner_promotion = promo_dict

    _emit_apply_trace(session_state, phase, result, promo=promo_dict)
    LOG.info(
        "planner_promotion_applied action=%s target=%s conf=%.2f",
        llm.action,
        result.get("target"),
        float(llm.confidence),
    )
    return result


def _emit_apply_trace(
    session_state: Any,
    phase: str,
    result: dict[str, Any],
    *,
    promo: dict[str, Any] | None = None,
) -> None:
    try:
        from explorer_trace import write_explorer_trace

        if result.get("applied"):
            event = "planner.promotion.applied"
        elif result.get("blocked"):
            event = "planner.promotion.blocked"
        else:
            event = "planner.promotion.skipped"
        write_explorer_trace(
            event,
            phase=phase,
            query=str(getattr(session_state, "current_query", "") or "")[:500],
            turn_index=int(getattr(session_state, "turn_index", 0) or 0),
            applied=result.get("applied"),
            skipped=result.get("skipped"),
            blocked=result.get("blocked"),
            effective_action=result.get("effective_action"),
            original_rule_action=result.get("original_rule_action"),
            promotion_source=result.get("promotion_source"),
            reason=result.get("reason"),
            confidence=result.get("confidence"),
            target=result.get("target"),
            promotion_decision=promo,
            result_summary=str(result.get("reason") or "")[:240],
        )
    except Exception as exc:
        LOG.debug("promotion apply trace failed: %s", exc)
