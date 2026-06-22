"""Context Runtime v1 — Core IP re-exports.

Direct imports from flat modules remain supported; this package
documents the product boundary for Context Runtime SKU.
"""

from __future__ import annotations

from typing import Any

from context_budget import (
    BudgetPlan,
    RetrievalStats,
    allocate_dynamic,
    allocate_static,
)
from context_need import (
    ContextNeed,
    build_context_need,
    extract_context_need,
    merge_context_need,
    validate_context_need,
)
from coverage_checker import CoverageReport, check_coverage
from recovery_scheduler import RecoveryResult, RecoveryScheduler
from runtime_turn_log import record_runtime_turn

from .prompt_enforcer import (
    emergency_shrink,
    enforce_prompt_budget,
    record_ctx_overflow,
    record_ctx_success,
)

__all__ = [
    "BudgetPlan",
    "RetrievalStats",
    "allocate_dynamic",
    "allocate_static",
    "ContextNeed",
    "build_context_need",
    "extract_context_need",
    "merge_context_need",
    "validate_context_need",
    "CoverageReport",
    "check_coverage",
    "build_context_for_turn",
    "PromptPack",
    "build_with_budget",
    "RecoveryResult",
    "RecoveryScheduler",
    "record_runtime_turn",
    "enforce_prompt_budget",
    "emergency_shrink",
    "record_ctx_overflow",
    "record_ctx_success",
]

TIER = "core_ip"
SKU = "context_runtime_v1"


def __getattr__(name: str) -> Any:
    if name == "build_context_for_turn":
        from dynamic_context_scheduler import build_context_for_turn

        return build_context_for_turn
    if name == "PromptPack":
        from prompt_builder import PromptPack

        return PromptPack
    if name == "build_with_budget":
        from prompt_builder import build_with_budget

        return build_with_budget
    raise AttributeError(name)
