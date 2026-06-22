"""Agent Brain — action types the AI Planner returns (Phase 2 target)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class PlannerAction(str, Enum):
    RETRIEVE_MORE = "retrieve_more"
    CALL_TOOL = "call_tool"
    SUMMARIZE_MEMORY = "summarize_memory"
    ASK_CLARIFICATION = "ask_clarification"
    FINAL_ANSWER = "final_answer"


@dataclass
class PlannerDecision:
    """Structured next step — replaces scattered FSM + rule next_action."""

    action: str
    reasoning: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    allow_final: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PlannerDecision | None:
        if not data or not data.get("action"):
            return None
        return cls(
            action=str(data.get("action") or ""),
            reasoning=str(data.get("reasoning") or ""),
            tool_name=str(data.get("tool_name") or ""),
            tool_args=dict(data.get("tool_args") or {}),
            confidence=float(data.get("confidence") or 0),
            allow_final=bool(data.get("allow_final")),
        )
