"""Runtime Kernel — SSOT for turn phase."""

from __future__ import annotations

from enum import Enum
from typing import Literal

# Canonical phase names for Runtime Kernel + Agent Brain + Observability.
RuntimePhaseName = Literal[
    "tool_planning",
    "exploration",
    "final_answer",
    "partial_final_answer",
    "recovery_final",
]


class RuntimePhase(str, Enum):
    TOOL_PLANNING = "tool_planning"
    EXPLORATION = "exploration"
    FINAL_ANSWER = "final_answer"
    PARTIAL_FINAL = "partial_final_answer"
    RECOVERY_FINAL = "recovery_final"

    @classmethod
    def normalize(cls, value: str | None) -> RuntimePhase:
        raw = (value or "").strip().lower()
        if not raw:
            return cls.TOOL_PLANNING
        if raw in ("explore", "exploration", "read_only_explore"):
            return cls.EXPLORATION
        for member in cls:
            if member.value == raw:
                return member
        return cls.TOOL_PLANNING

    @property
    def is_final(self) -> bool:
        return self in (RuntimePhase.FINAL_ANSWER, RuntimePhase.PARTIAL_FINAL, RuntimePhase.RECOVERY_FINAL)

    @property
    def needs_tools(self) -> bool:
        return self in (RuntimePhase.TOOL_PLANNING, RuntimePhase.EXPLORATION)

    def to_legacy_agent_phase(self) -> str:
        return self.value


FINAL_PHASES = frozenset({
    RuntimePhase.FINAL_ANSWER.value,
    RuntimePhase.PARTIAL_FINAL.value,
    RuntimePhase.RECOVERY_FINAL.value,
})
