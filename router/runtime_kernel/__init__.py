"""Runtime Kernel — Context / Memory / Budget / Retrieval / Coverage orchestration SSOT."""

from __future__ import annotations

from .constants import (
    AGENT_FAST_FORBIDDEN,
    BACKEND_CTX_TOKENS,
    COVERAGE_CHECK,
    CTX_SAFETY_TOKENS,
    DEFAULT_PACK_BUDGET,
    DYNAMIC_BUDGET,
    EXEC_CONTEXT_INTENTS,
    EXEC_INTENTS,
    FAST_ONLY_INTENTS,
    FINAL_ANSWER_FAST_THRESHOLD,
    INTENT_BUDGET_TOKENS,
    MEMORY_STATE_BODY,
    MIN_TOOL_CALLS_FOR_FINAL_ANSWER,
    RECENT_AGENT_MSG_CHARS,
    RECENT_AGENT_MSG_KEEP,
    RECOVERY_ENABLED,
    TOKEN_THRESHOLD,
    TOOL_PLANNING_MAX_TOKENS,
    TOOL_TAIL_MAX_CHARS,
)
from .intent import RuntimeIntentResolution, resolve_runtime_intent
from .phase import FINAL_PHASES, RuntimePhase
from .runtime_state import RuntimeState, build_runtime_state, persist_runtime_state
from .self_model import format_self_model_block, load_self_model

__all__ = [
    "AGENT_FAST_FORBIDDEN",
    "BACKEND_CTX_TOKENS",
    "COVERAGE_CHECK",
    "CTX_SAFETY_TOKENS",
    "DEFAULT_PACK_BUDGET",
    "DYNAMIC_BUDGET",
    "EXEC_CONTEXT_INTENTS",
    "EXEC_INTENTS",
    "FAST_ONLY_INTENTS",
    "FINAL_ANSWER_FAST_THRESHOLD",
    "FINAL_PHASES",
    "INTENT_BUDGET_TOKENS",
    "MEMORY_STATE_BODY",
    "MIN_TOOL_CALLS_FOR_FINAL_ANSWER",
    "RECENT_AGENT_MSG_CHARS",
    "RECENT_AGENT_MSG_KEEP",
    "RECOVERY_ENABLED",
    "RuntimeIntentResolution",
    "RuntimePhase",
    "RuntimeState",
    "TOKEN_THRESHOLD",
    "TOOL_PLANNING_MAX_TOKENS",
    "TOOL_TAIL_MAX_CHARS",
    "build_runtime_state",
    "format_self_model_block",
    "load_self_model",
    "persist_runtime_state",
    "resolve_runtime_intent",
]
