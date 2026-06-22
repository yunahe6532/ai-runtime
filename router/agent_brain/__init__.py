"""Agent Brain — AI decision contract (Phase 2)."""

from __future__ import annotations

from .llm_planner import (
    LLM_PLANNER_MAX_TOKENS,
    LLM_PLANNER_SHADOW_ENABLED,
    LLM_PLANNER_TIMEOUT_SEC,
    llm_planner_shadow_enabled,
    propose_llm_shadow_decision,
)
from .planner_contract import PlannerAction, PlannerDecision, tool_to_action
from .planner_shadow import (
    compare_shadow_decisions,
    compare_triple_decisions,
    planner_shadow_enabled,
    propose_shadow_decision,
    rule_decision_from_plan,
    run_planner_shadow,
    run_planner_shadow_if_enabled,
)
from .promotion_gate import (
    PromotionDecision,
    evaluate_promotion,
    promotion_gate_enabled,
    promotion_metrics_snapshot,
    promotion_min_confidence,
    promotion_shadow_only,
    reset_promotion_metrics,
)
from .runtime_state import (
    MAX_RUNTIME_STATE_ANCHOR_ITEMS,
    MAX_RUNTIME_STATE_JOURNAL_ITEMS,
    MAX_RUNTIME_STATE_PROMPT_CHARS,
    MAX_RUNTIME_STATE_WS_ITEMS,
    RuntimeState,
    RuntimeStateBuilder,
    RuntimeStateLimits,
    persist_planner_runtime_state,
)

__all__ = [
    "PlannerAction",
    "PlannerDecision",
    "tool_to_action",
    "RuntimeState",
    "RuntimeStateBuilder",
    "RuntimeStateLimits",
    "persist_planner_runtime_state",
    "MAX_RUNTIME_STATE_PROMPT_CHARS",
    "MAX_RUNTIME_STATE_JOURNAL_ITEMS",
    "MAX_RUNTIME_STATE_ANCHOR_ITEMS",
    "MAX_RUNTIME_STATE_WS_ITEMS",
    "planner_shadow_enabled",
    "llm_planner_shadow_enabled",
    "LLM_PLANNER_SHADOW_ENABLED",
    "LLM_PLANNER_TIMEOUT_SEC",
    "LLM_PLANNER_MAX_TOKENS",
    "run_planner_shadow",
    "run_planner_shadow_if_enabled",
    "rule_decision_from_plan",
    "propose_shadow_decision",
    "propose_llm_shadow_decision",
    "compare_shadow_decisions",
    "compare_triple_decisions",
    "PromotionDecision",
    "evaluate_promotion",
    "promotion_gate_enabled",
    "promotion_shadow_only",
    "promotion_min_confidence",
    "promotion_metrics_snapshot",
    "reset_promotion_metrics",
]
