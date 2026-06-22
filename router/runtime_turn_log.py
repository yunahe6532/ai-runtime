"""Per-turn runtime metrics for inspector, bench, and post-mortem."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

LOG = logging.getLogger("router.runtime_turn")

COVERAGE_THRESHOLD = float(os.getenv("COVERAGE_THRESHOLD", "0.75"))


def record_runtime_turn(
    state: Any,
    *,
    flow_id: str = "",
    intent: str = "",
    phase: str = "",
    context_need: Any = None,
    budget_plan: Any = None,
    retrieval_pack: Any = None,
    coverage: Any = None,
    recovery_triggered: bool = False,
    recovery_recovered: bool = False,
    recovery_rounds: int = 0,
    final_blocked_reason: str = "",
    dynamic_budget_enabled: bool | None = None,
) -> dict[str, Any]:
    """Persist turn snapshot on session state and emit structured log."""
    need_dict = (
        context_need.to_dict()
        if hasattr(context_need, "to_dict")
        else dict(context_need or {})
    )
    budget_dict = (
        budget_plan.to_dict()
        if hasattr(budget_plan, "to_dict")
        else dict(budget_plan or {})
    )
    coverage_dict = (
        coverage.to_dict()
        if hasattr(coverage, "to_dict")
        else dict(coverage or {})
    )

    items = getattr(retrieval_pack, "items", None) or []
    retrieval_items = [
        {
            "source": getattr(i, "source", ""),
            "tokens": getattr(i, "tokens", 0),
            "score": getattr(i, "score", 0),
            "section": getattr(i, "section", ""),
            "must_include": getattr(i, "must_include", False),
        }
        for i in items[:12]
    ]

    turn = {
        "flow_id": flow_id or getattr(state, "last_run_id", "") or "",
        "intent": intent or need_dict.get("intent", ""),
        "phase": phase or "",
        "dynamic_budget_enabled": dynamic_budget_enabled,
        "context_need": need_dict,
        "budget_plan": budget_dict,
        "retrieval_total_tokens": int(getattr(retrieval_pack, "total_tokens", 0) or 0),
        "retrieval_items": retrieval_items,
        "retrieval_missing_targets": list(getattr(retrieval_pack, "missing_targets", None) or []),
        "coverage_score": float(coverage_dict.get("coverage_score", 1.0) or 1.0),
        "coverage_complete": bool(coverage_dict.get("complete", True)),
        "coverage_missing": list(coverage_dict.get("missing") or []),
        "coverage_truncated": list(coverage_dict.get("truncated") or []),
        "coverage_action": str(coverage_dict.get("action") or "proceed"),
        "critical_source_truncated": bool(coverage_dict.get("critical_source_truncated")),
        "latest_tool_result_missing": bool(coverage_dict.get("latest_tool_result_missing")),
        "recovery_triggered": recovery_triggered,
        "recovery_recovered": recovery_recovered,
        "recovery_rounds": recovery_rounds,
        "final_blocked_reason": final_blocked_reason or "",
        "memory_hierarchy": dict(getattr(state, "last_memory_hierarchy", None) or {}),
    }

    if state is not None:
        state.last_runtime_turn = turn
        shadow = dict(getattr(state, "last_planner_shadow", None) or {})
        if shadow:
            turn["planner_shadow_decision"] = shadow
            turn["planner_observability"] = {
                "match": shadow.get("match"),
                "mismatch_reason": shadow.get("mismatch_reason"),
                "would_change_hot_path": shadow.get("would_change_hot_path"),
                "target_overlap": shadow.get("target_overlap"),
                "evidence_overlap": shadow.get("evidence_overlap"),
                "confidence_delta": shadow.get("confidence_delta"),
                "rule_action": (shadow.get("comparison") or {}).get("rule_action"),
                "heuristic_action": (shadow.get("comparison") or {}).get("shadow_action"),
            }
            llm_shadow = dict(getattr(state, "last_planner_llm_shadow", None) or {})
            if not llm_shadow and shadow.get("llm_shadow_decision"):
                llm_shadow = {
                    "decision": shadow.get("llm_shadow_decision"),
                    "meta": shadow.get("llm_shadow_meta"),
                    "triple_comparison": shadow.get("triple_comparison"),
                }
            if llm_shadow:
                turn["planner_llm_shadow"] = llm_shadow
                triple = dict(llm_shadow.get("triple_comparison") or shadow.get("triple_comparison") or {})
                turn["planner_observability"].update({
                    "llm_action": triple.get("llm_action") or (llm_shadow.get("decision") or {}).get("action"),
                    "llm_status": (llm_shadow.get("meta") or shadow.get("llm_shadow_meta") or {}).get("status"),
                    "action_match_rule_llm": triple.get("action_match_rule_llm"),
                    "triple_would_change_hot_path": triple.get("would_change_hot_path"),
                })
            promo = dict(
                getattr(state, "last_planner_promotion", None)
                or shadow.get("promotion_decision")
                or (getattr(state, "last_runtime_turn", None) or {}).get("planner_promotion_decision")
                or {}
            )
            if promo:
                turn["planner_promotion_decision"] = promo
                turn["planner_observability"].update({
                    "promotion_eligible": promo.get("eligible"),
                    "promotion_allowed_action": promo.get("allowed_action"),
                    "promotion_blocked_reasons": promo.get("blocked_reasons"),
                    "promotion_would_change_hot_path": promo.get("would_change_hot_path"),
                    "promotion_apply_allowed": promo.get("apply_allowed"),
                    "promotion_apply_reason": promo.get("apply_reason"),
                })
            rt_promo = dict(getattr(state, "last_runtime_turn", None) or {})
            if rt_promo.get("planner_promotion_applied"):
                turn["planner_promotion_applied"] = True
                turn["effective_action"] = rt_promo.get("effective_action")
                turn["original_rule_action"] = rt_promo.get("original_rule_action")
                turn["promotion_source"] = rt_promo.get("promotion_source")
                turn["planner_observability"].update({
                    "planner_promotion_applied": True,
                    "effective_action": rt_promo.get("effective_action"),
                    "original_rule_action": rt_promo.get("original_rule_action"),
                    "promotion_source": rt_promo.get("promotion_source"),
                })
        fr = dict(getattr(state, "last_runtime_turn", None) or {})
        if fr.get("final_report_used") is not None:
            turn["final_report_used"] = fr.get("final_report_used")
            turn["final_report_chars"] = fr.get("final_report_chars")
        metrics = dict(getattr(state, "last_ingest_metrics", None) or {})
        metrics.update({
            "coverage_score": turn["coverage_score"],
            "coverage_complete": turn["coverage_complete"],
            "coverage_missing": turn["coverage_missing"],
            "recovery_triggered": recovery_triggered,
            "recovery_recovered": recovery_recovered,
            "recovery_rounds": recovery_rounds,
            "retrieval_total_tokens": turn["retrieval_total_tokens"],
            "budget_mode": budget_dict.get("mode", ""),
            "final_blocked_reason": final_blocked_reason,
        })
        state.last_ingest_metrics = metrics

    LOG.info("runtime_turn %s", json.dumps(turn, ensure_ascii=False, default=str))

    try:
        from adapters.observe import emit_langfuse_event

        emit_langfuse_event("runtime_turn", turn)
    except ImportError:
        pass

    return turn
