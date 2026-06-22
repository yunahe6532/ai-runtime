"""Agent Runtime v2 — Reference integration (Cursor POC)."""

from .agent_exec import guard_tool_calls_in_response
from .evidence_judge import evaluate_exploration, should_run_judge_batch
from .loop_guard import should_block_final_answer
from .plan_state import resolve_agent_phase
from .planner import AgentPlan, ensure_agent_plan, normalize_plan, validate_tool_call

__all__ = [
    "AgentPlan",
    "ensure_agent_plan",
    "normalize_plan",
    "validate_tool_call",
    "guard_tool_calls_in_response",
    "evaluate_exploration",
    "should_run_judge_batch",
    "should_block_final_answer",
    "resolve_agent_phase",
]

TIER = "reference_integration"
SKU = "agent_runtime_v2"
