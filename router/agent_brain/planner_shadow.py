"""Agent Brain — Planner shadow mode (Phase 2.0: observe only, no hot path change)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .planner_contract import PlannerDecision, tool_to_action
from .runtime_state import RuntimeState, RuntimeStateBuilder, persist_planner_runtime_state

LOG = logging.getLogger("agent_brain.planner_shadow")

PLANNER_SHADOW_MODE = os.getenv("PLANNER_SHADOW_MODE", "1") != "0"


def planner_shadow_enabled() -> bool:
    return PLANNER_SHADOW_MODE


def rule_decision_from_plan(agent_plan: dict[str, Any], *, phase: str = "") -> PlannerDecision:
    """Baseline from existing rule planner next_action."""
    na = dict(agent_plan.get("next_action") or {})
    tool = str(na.get("tool") or "")
    action = tool_to_action(tool, phase=phase)
    target = str(na.get("target") or na.get("path") or "")
    return PlannerDecision(
        action=action,
        target_files=[target] if target else [],
        reason=str(na.get("reason") or "rule_planner"),
        evidence_needed=list(agent_plan.get("evidence_needed") or [])[:12],
        confidence=float(agent_plan.get("confidence") or 0.7),
        tool_name=tool,
        tool_args={
            k: na[k]
            for k in ("path", "pattern", "command", "glob_pattern", "target_directory")
            if na.get(k)
        },
    )


def propose_shadow_decision(runtime_state: RuntimeState) -> PlannerDecision:
    """Heuristic shadow candidate — Phase 2.0 stub until LLM planner (2.1)."""
    constraints = runtime_state.constraints or {}
    needed = list(constraints.get("evidence_needed") or [])
    collected = set(str(x) for x in (constraints.get("evidence_collected") or []))
    missing = [n for n in needed if not any(str(n).split(":")[0] in c for c in collected)]

    phase = runtime_state.phase or "tool_planning"
    if phase in ("final_answer", "partial_final_answer", "recovery_final"):
        if not missing and constraints.get("final_ready"):
            return PlannerDecision(
                action="final",
                reason="shadow: final phase with evidence ready",
                confidence=0.85,
                stop_condition="coverage_satisfied",
            )
        return PlannerDecision(
            action="summarize",
            reason="shadow: final phase, synthesize from journal",
            confidence=0.7,
            evidence_needed=missing[:6],
        )

    if not constraints.get("coverage_complete", True):
        ws = runtime_state.working_set_summary or {}
        targets = list(ws.get("priority_targets") or ws.get("must_include") or [])
        if targets:
            return PlannerDecision(
                action="read",
                target_files=targets[:3],
                reason="shadow: coverage incomplete — read working set target",
                evidence_needed=missing[:6],
                confidence=0.75,
                risk_flags=["coverage_gap"] if missing else [],
            )
        return PlannerDecision(
            action="recover",
            reason="shadow: coverage incomplete, no WS target",
            evidence_needed=missing[:6],
            confidence=0.6,
            risk_flags=["coverage_gap"],
        )

    raw_na = (runtime_state.raw or {}).get("agent_plan_excerpt", {}).get("next_action") or {}
    tool = str(raw_na.get("tool") or "")
    if tool:
        action = tool_to_action(tool, phase=phase)
        target = str(raw_na.get("target") or raw_na.get("path") or "")
        return PlannerDecision(
            action=action,
            target_files=[target] if target else [],
            reason=f"shadow: align rule next_action ({tool})",
            evidence_needed=missing[:6],
            confidence=0.8,
            tool_name=tool,
        )

    journal = runtime_state.task_journal_tail or []
    if journal and journal[-1].get("kind") == "failure":
        return PlannerDecision(
            action="recover",
            reason="shadow: last tool failed",
            confidence=0.65,
            risk_flags=["last_tool_failed"],
        )

    files = list((runtime_state.raw or {}).get("files_read") or [])
    if not files and "read" in (runtime_state.available_actions or []):
        eps = list((runtime_state.project_index_summary or {}).get("entrypoints") or [])
        return PlannerDecision(
            action="read",
            target_files=eps[:2] or ["README.md"],
            reason="shadow: bootstrap read from index entrypoints",
            confidence=0.7,
        )

    return PlannerDecision(
        action="grep",
        target_files=[],
        target_symbols=["def ", "class "],
        reason="shadow: default explore",
        confidence=0.55,
    )


def _overlap(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def compare_shadow_decisions(
    rule: PlannerDecision,
    shadow: PlannerDecision,
    *,
    phase: str = "",
    label_a: str = "rule",
    label_b: str = "shadow",
) -> dict[str, Any]:
    mismatch_reasons: list[str] = []

    if rule.action != shadow.action:
        mismatch_reasons.append("action_mismatch")

    rule_targets = {t.lower() for t in rule.target_files if t}
    shadow_targets = {t.lower() for t in shadow.target_files if t}
    target_overlap = _overlap(rule_targets, shadow_targets)
    if rule_targets and shadow_targets and not (rule_targets & shadow_targets):
        mismatch_reasons.append("target_mismatch")
    elif rule_targets != shadow_targets and (rule_targets or shadow_targets):
        if "target_mismatch" not in mismatch_reasons and rule_targets and shadow_targets:
            mismatch_reasons.append("target_mismatch")

    rule_ev = {str(x).split(":")[0] for x in rule.evidence_needed}
    shadow_ev = {str(x).split(":")[0] for x in shadow.evidence_needed}
    evidence_overlap = _overlap(rule_ev, shadow_ev)
    if rule_ev and shadow_ev and rule_ev != shadow_ev:
        mismatch_reasons.append("evidence_mismatch")

    final_phases = frozenset({"final_answer", "partial_final_answer", "recovery_final"})
    rule_final = rule.action == "final"
    shadow_final = shadow.action == "final"
    if phase in final_phases and rule_final != shadow_final:
        mismatch_reasons.append("phase_mismatch")

    confidence_delta = round(float(shadow.confidence) - float(rule.confidence), 3)
    would_change_hot_path = bool(mismatch_reasons) and (
        rule.action != shadow.action
        or (rule_targets != shadow_targets and rule_targets and shadow_targets)
    )

    return {
        "match": len(mismatch_reasons) == 0,
        "action_match": rule.action == shadow.action,
        "mismatch_reasons": mismatch_reasons,
        "mismatch_reason": ",".join(mismatch_reasons),
        f"{label_a}_action": rule.action,
        f"{label_b}_action": shadow.action,
        "rule_action": rule.action,
        "shadow_action": shadow.action,
        "rule_targets": rule.target_files,
        "shadow_targets": shadow.target_files,
        "rule_decision": rule.to_dict(),
        "shadow_decision": shadow.to_dict(),
        "confidence_delta": confidence_delta,
        "target_overlap": round(target_overlap, 3),
        "evidence_overlap": round(evidence_overlap, 3),
        "risk_flags_rule": list(rule.risk_flags),
        "risk_flags_shadow": list(shadow.risk_flags),
        "would_change_hot_path": would_change_hot_path,
        "phase": phase,
        "label_a": label_a,
        "label_b": label_b,
    }


def compare_triple_decisions(
    rule: PlannerDecision,
    heuristic: PlannerDecision,
    llm: PlannerDecision,
    *,
    phase: str = "",
) -> dict[str, Any]:
    """Rule vs heuristic vs LLM shadow comparison."""
    rh = compare_shadow_decisions(rule, heuristic, phase=phase, label_a="rule", label_b="heuristic")
    rl = compare_shadow_decisions(rule, llm, phase=phase, label_a="rule", label_b="llm")
    hl = compare_shadow_decisions(heuristic, llm, phase=phase, label_a="heuristic", label_b="llm")

    would_change = any(
        x.get("would_change_hot_path")
        for x in (rh, rl, hl)
    )

    return {
        "rule_action": rule.action,
        "heuristic_action": heuristic.action,
        "llm_action": llm.action,
        "action_match_rule_heuristic": rh.get("action_match"),
        "action_match_rule_llm": rl.get("action_match"),
        "action_match_heuristic_llm": hl.get("action_match"),
        "target_overlap_rule_llm": rl.get("target_overlap"),
        "target_overlap_heuristic_llm": hl.get("target_overlap"),
        "evidence_overlap_rule_llm": rl.get("evidence_overlap"),
        "confidence_delta_rule_llm": rl.get("confidence_delta"),
        "confidence_delta_heuristic_llm": hl.get("confidence_delta"),
        "risk_flags_rule": list(rule.risk_flags),
        "risk_flags_heuristic": list(heuristic.risk_flags),
        "risk_flags_llm": list(llm.risk_flags),
        "would_change_hot_path": would_change,
        "rule_vs_heuristic": rh,
        "rule_vs_llm": rl,
        "heuristic_vs_llm": hl,
        "phase": phase,
    }


def run_planner_shadow(
    session_state: Any,
    *,
    query: str = "",
    phase: str = "tool_planning",
    router_intent: str = "",
    context_intent: str = "",
    project_index: Any | None = None,
    working_set: Any | None = None,
    budget_plan: Any | None = None,
    coverage: Any | None = None,
    context_need: Any | None = None,
) -> dict[str, Any]:
    """Build RuntimeState, run shadow decision, compare to rule planner — no execution change."""
    builder = RuntimeStateBuilder()
    brain_state = builder.build(
        session_state=session_state,
        query=query,
        phase=phase,
        router_intent=router_intent,
        context_intent=context_intent,
        project_index=project_index,
        working_set=working_set,
        budget_plan=budget_plan,
        coverage=coverage,
        context_need=context_need,
    )
    persist_planner_runtime_state(session_state, brain_state)

    ap = dict(getattr(session_state, "agent_plan", None) or {})
    rule = rule_decision_from_plan(ap, phase=phase)
    heuristic = propose_shadow_decision(brain_state)
    comparison = compare_shadow_decisions(rule, heuristic, phase=phase)

    llm_decision: PlannerDecision | None = None
    llm_meta: dict[str, Any] = {"enabled": False, "status": "disabled"}
    triple_comparison: dict[str, Any] | None = None

    try:
        from .llm_planner import llm_planner_shadow_enabled, propose_llm_shadow_decision

        if llm_planner_shadow_enabled():
            llm_decision, llm_meta = propose_llm_shadow_decision(brain_state)
            triple_comparison = compare_triple_decisions(rule, heuristic, llm_decision, phase=phase)
            session_state.last_planner_llm_shadow = {
                "decision": llm_decision.to_dict(),
                "meta": llm_meta,
                "triple_comparison": triple_comparison,
            }
    except Exception as exc:
        llm_meta = {"enabled": True, "status": "error", "error": str(exc)[:200]}
        LOG.warning("llm planner shadow failed: %s", exc)

    promotion_decision: dict[str, Any] | None = None
    try:
        from .promotion_gate import evaluate_promotion, promotion_gate_enabled, build_effective_planner_action

        if promotion_gate_enabled() and llm_decision is not None:
            promo = evaluate_promotion(
                rule, heuristic, llm_decision, brain_state, phase=phase, session_state=session_state,
            )
            promotion_decision = promo.to_dict()
            session_state.last_planner_promotion = promotion_decision
            if promo.apply_allowed:
                effective = build_effective_planner_action(llm_decision, rule, promo)
                session_state.last_effective_planner_action = effective.to_dict()
    except Exception as exc:
        LOG.warning("promotion gate failed: %s", exc)
        promotion_decision = {"eligible": False, "reason": f"error:{exc}", "blocked_reasons": ["error"]}

    payload = {
        "shadow_mode": True,
        "runtime_state_prompt_chars": len(brain_state.to_prompt_json()),
        "rule_decision": rule.to_dict(),
        "heuristic_shadow_decision": heuristic.to_dict(),
        "shadow_decision": heuristic.to_dict(),
        "comparison": comparison,
        "match": comparison.get("match"),
        "mismatch_reason": comparison.get("mismatch_reason") or "",
        "confidence_delta": comparison.get("confidence_delta"),
        "target_overlap": comparison.get("target_overlap"),
        "evidence_overlap": comparison.get("evidence_overlap"),
        "would_change_hot_path": comparison.get("would_change_hot_path"),
        "llm_shadow_decision": llm_decision.to_dict() if llm_decision else None,
        "llm_shadow_meta": llm_meta,
        "triple_comparison": triple_comparison,
        "promotion_decision": promotion_decision,
    }

    effective_planner_action = getattr(session_state, "last_effective_planner_action", None)
    if effective_planner_action:
        payload["effective_planner_action"] = effective_planner_action

    if triple_comparison:
        payload["would_change_hot_path"] = triple_comparison.get("would_change_hot_path")

    session_state.last_planner_shadow = payload
    rt = dict(getattr(session_state, "last_runtime_turn", None) or {})
    rt["planner_shadow_decision"] = payload
    if llm_decision:
        rt["planner_llm_shadow"] = {
            "decision": llm_decision.to_dict(),
            "meta": llm_meta,
            "triple_comparison": triple_comparison,
        }
    if promotion_decision:
        rt["planner_promotion_decision"] = promotion_decision
    session_state.last_runtime_turn = rt

    _emit_planner_trace_events(
        query=query,
        phase=phase,
        turn_index=int(getattr(session_state, "turn_index", 0) or 0),
        brain_state=brain_state,
        rule=rule,
        heuristic=heuristic,
        llm=llm_decision,
        comparison=comparison,
        triple_comparison=triple_comparison,
        llm_meta=llm_meta,
        promotion_decision=promotion_decision,
        payload=payload,
    )

    LOG.info(
        "planner_shadow match=%s rule=%s heuristic=%s llm=%s mismatch=%s would_change_hot_path=%s prompt_chars=%d",
        comparison.get("match"),
        rule.action,
        heuristic.action,
        llm_decision.action if llm_decision else "n/a",
        payload.get("mismatch_reason") or "none",
        payload.get("would_change_hot_path"),
        payload.get("runtime_state_prompt_chars"),
    )
    return payload


def _emit_planner_trace_events(
    *,
    query: str,
    phase: str,
    turn_index: int,
    brain_state: RuntimeState,
    rule: PlannerDecision,
    heuristic: PlannerDecision,
    comparison: dict[str, Any],
    payload: dict[str, Any],
    llm: PlannerDecision | None = None,
    triple_comparison: dict[str, Any] | None = None,
    llm_meta: dict[str, Any] | None = None,
    promotion_decision: dict[str, Any] | None = None,
) -> None:
    try:
        from explorer_trace import write_explorer_trace

        summary = (
            f"intent={brain_state.router_intent} journal={len(brain_state.task_journal_tail)} "
            f"anchors={len(brain_state.evidence_anchor_summary)}"
        )
        write_explorer_trace(
            "planner.runtime_state.created",
            phase=phase,
            query=query,
            turn_index=turn_index,
            result_summary=summary,
            runtime_state_summary=summary,
            runtime_state_prompt_chars=payload.get("runtime_state_prompt_chars"),
        )
        write_explorer_trace(
            "planner.shadow.proposed",
            phase=phase,
            query=query,
            turn_index=turn_index,
            decision=heuristic.action,
            action=heuristic.action,
            target_files=heuristic.target_files,
            target_symbols=heuristic.target_symbols,
            reason=heuristic.reason,
            evidence_needed=heuristic.evidence_needed,
            confidence=heuristic.confidence,
            tool_name=heuristic.tool_name,
            tool_args=heuristic.tool_args,
            planner_kind="heuristic",
        )
        write_explorer_trace(
            "planner.shadow.compared",
            phase=phase,
            query=query,
            turn_index=turn_index,
            decision=heuristic.action,
            reason=heuristic.reason,
            match=comparison.get("match"),
            mismatch_reason=comparison.get("mismatch_reason"),
            mismatch_reasons=comparison.get("mismatch_reasons"),
            rule_action=rule.action,
            shadow_action=heuristic.action,
            target_files=heuristic.target_files,
            confidence_delta=comparison.get("confidence_delta"),
            target_overlap=comparison.get("target_overlap"),
            evidence_overlap=comparison.get("evidence_overlap"),
            would_change_hot_path=comparison.get("would_change_hot_path"),
            result_summary=(
                f"rule={rule.action} heuristic={heuristic.action} "
                f"overlap_tgt={comparison.get('target_overlap')} "
                f"would_change={comparison.get('would_change_hot_path')}"
            ),
        )
        if llm is not None:
            write_explorer_trace(
                "planner.llm.proposed",
                phase=phase,
                query=query,
                turn_index=turn_index,
                decision=llm.action,
                action=llm.action,
                target_files=llm.target_files,
                target_symbols=llm.target_symbols,
                reason=llm.reason,
                evidence_needed=llm.evidence_needed,
                confidence=llm.confidence,
                risk_flags=llm.risk_flags,
                llm_status=(llm_meta or {}).get("status"),
                result_summary=f"llm={llm.action} status={(llm_meta or {}).get('status')}",
            )
        if triple_comparison and llm is not None:
            write_explorer_trace(
                "planner.triple_compared",
                phase=phase,
                query=query,
                turn_index=turn_index,
                rule_action=triple_comparison.get("rule_action"),
                heuristic_action=triple_comparison.get("heuristic_action"),
                llm_action=triple_comparison.get("llm_action"),
                action_match_rule_llm=triple_comparison.get("action_match_rule_llm"),
                action_match_heuristic_llm=triple_comparison.get("action_match_heuristic_llm"),
                target_overlap_rule_llm=triple_comparison.get("target_overlap_rule_llm"),
                evidence_overlap_rule_llm=triple_comparison.get("evidence_overlap_rule_llm"),
                confidence_delta_rule_llm=triple_comparison.get("confidence_delta_rule_llm"),
                would_change_hot_path=triple_comparison.get("would_change_hot_path"),
                risk_flags_llm=triple_comparison.get("risk_flags_llm"),
                result_summary=(
                    f"rule={triple_comparison.get('rule_action')} "
                    f"heuristic={triple_comparison.get('heuristic_action')} "
                    f"llm={triple_comparison.get('llm_action')}"
                ),
            )
        if promotion_decision is not None:
            event = (
                "planner.promotion.eligible"
                if promotion_decision.get("eligible")
                else "planner.promotion.blocked"
            )
            write_explorer_trace(
                "planner.promotion.evaluated",
                phase=phase,
                query=query,
                turn_index=turn_index,
                eligible=promotion_decision.get("eligible"),
                allowed_action=promotion_decision.get("allowed_action"),
                reason=promotion_decision.get("reason"),
                blocked_reasons=promotion_decision.get("blocked_reasons"),
                confidence=promotion_decision.get("confidence"),
                target_overlap=promotion_decision.get("target_overlap"),
                evidence_overlap=promotion_decision.get("evidence_overlap"),
                risk_flags=promotion_decision.get("risk_flags"),
                would_change_hot_path=promotion_decision.get("would_change_hot_path"),
                shadow_only=promotion_decision.get("shadow_only"),
                dry_run_tool_call=promotion_decision.get("dry_run_tool_call"),
                metrics=promotion_decision.get("metrics"),
                result_summary=str(promotion_decision.get("reason") or "")[:240],
            )
            write_explorer_trace(
                event,
                phase=phase,
                query=query,
                turn_index=turn_index,
                eligible=promotion_decision.get("eligible"),
                allowed_action=promotion_decision.get("allowed_action"),
                reason=promotion_decision.get("reason"),
                blocked_reasons=promotion_decision.get("blocked_reasons"),
                dry_run_tool_call=promotion_decision.get("dry_run_tool_call"),
                would_change_hot_path=promotion_decision.get("would_change_hot_path"),
                result_summary=(
                    f"eligible={promotion_decision.get('eligible')} "
                    f"action={promotion_decision.get('allowed_action')}"
                ),
            )
    except Exception as exc:
        LOG.warning("planner trace emit failed: %s", exc)


def run_planner_shadow_if_enabled(session_state: Any, **kwargs: Any) -> dict[str, Any] | None:
    if not planner_shadow_enabled() or session_state is None:
        return None
    try:
        return run_planner_shadow(session_state, **kwargs)
    except Exception as exc:
        LOG.warning("planner_shadow failed: %s", exc)
        return None
