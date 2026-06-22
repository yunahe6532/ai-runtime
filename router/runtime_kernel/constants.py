"""Runtime Kernel — SSOT for budget and env constants."""

from __future__ import annotations

import os

# --- Context window ---
def _long_ctx_default() -> int:
    if os.getenv("LONG_CTX_TOKENS"):
        return int(os.getenv("LONG_CTX_TOKENS", "32768"))
    return int(os.getenv("LONG_CONTEXT_SIZE", "32768"))


BACKEND_CTX_TOKENS: dict[str, int] = {
    "fast": int(os.getenv("FAST_CTX_TOKENS", os.getenv("FAST_CONTEXT_SIZE", "24576"))),
    "long": _long_ctx_default(),
    "vl": int(os.getenv("VL_CTX_TOKENS", os.getenv("VL_CONTEXT_SIZE", "32768"))),
}

CTX_SAFETY_TOKENS = int(os.getenv("CTX_SAFETY_TOKENS", "2000"))
TOKEN_THRESHOLD = int(os.getenv("TOKEN_THRESHOLD", "20000"))

# --- Tool / phase budgets (single default: docker-compose uses 800) ---
TOOL_PLANNING_MAX_TOKENS = int(os.getenv("TOOL_PLANNING_MAX_TOKENS", "800"))
FINAL_ANSWER_FAST_THRESHOLD = int(os.getenv("FINAL_ANSWER_FAST_THRESHOLD", "6000"))
INTENT_BUDGET_TOKENS = int(os.getenv("INTENT_BUDGET_TOKENS", "8000"))
DEFAULT_PACK_BUDGET = int(os.getenv("CONTEXT_PACK_BUDGET", "12000"))

# --- Agent loop ---
MIN_TOOL_CALLS_FOR_FINAL_ANSWER = int(os.getenv("MIN_TOOL_CALLS_FOR_FINAL_ANSWER", "3"))
RECENT_AGENT_MSG_KEEP = int(os.getenv("RECENT_AGENT_MSG_KEEP", "8"))
RECENT_AGENT_MSG_CHARS = int(os.getenv("RECENT_AGENT_MSG_CHARS", "8000"))
TOOL_TAIL_MAX_CHARS = int(os.getenv("TOOL_TAIL_MAX_CHARS", "1200"))

# --- Runtime pipeline flags ---
DYNAMIC_BUDGET = os.getenv("DYNAMIC_BUDGET", "1") == "1"
COVERAGE_CHECK = os.getenv("COVERAGE_CHECK", "1") == "1"
RECOVERY_ENABLED = os.getenv("RECOVERY_ENABLED", "1") == "1"
MEMORY_STATE_BODY = os.getenv("MEMORY_STATE_BODY", "1") == "1"

# --- Exec intents (single set for stream/tools/session policy) ---
EXEC_INTENTS = frozenset({
    "shell_task",
    "benchmark",
    "log_analysis",
    "code_edit",
    "bugfix",
})
EXEC_CONTEXT_INTENTS = frozenset(EXEC_INTENTS | {"agent", "debug"})
AGENT_FAST_FORBIDDEN = frozenset({
    "shell_task",
    "log_analysis",
    "benchmark",
    "code_edit",
    "debug",
})
FAST_ONLY_INTENTS = frozenset({"casual", "explain"})
