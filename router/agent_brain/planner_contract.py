"""Agent Brain — PlannerDecision contract (Phase 2.0)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class PlannerAction(str, Enum):
    READ = "read"
    GREP = "grep"
    GLOB = "glob"
    SHELL = "shell"
    EDIT = "edit"
    SUMMARIZE = "summarize"
    FINAL = "final"
    ASK_USER = "ask_user"
    RECOVER = "recover"

    # Legacy aliases (Phase 1.x)
    RETRIEVE_MORE = "retrieve_more"
    CALL_TOOL = "call_tool"
    SUMMARIZE_MEMORY = "summarize_memory"
    ASK_CLARIFICATION = "ask_clarification"
    FINAL_ANSWER = "final_answer"


VALID_ACTIONS = frozenset(a.value for a in PlannerAction if not a.value.endswith("_more"))


@dataclass
class PlannerDecision:
    """Structured next step for AI Planner (shadow → hot path in Phase 2.1+)."""

    action: str
    target_files: list[str] = field(default_factory=list)
    target_symbols: list[str] = field(default_factory=list)
    reason: str = ""
    evidence_needed: list[str] = field(default_factory=list)
    confidence: float = 0.0
    stop_condition: str = ""
    risk_flags: list[str] = field(default_factory=list)
    # Legacy / tool bridge
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reasoning"] = self.reason
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PlannerDecision | None:
        if not data:
            return None
        action = str(data.get("action") or "").strip().lower()
        if not action:
            return None
        action = _normalize_action(action)
        return cls(
            action=action,
            target_files=[str(x) for x in (data.get("target_files") or data.get("targets") or []) if x],
            target_symbols=[str(x) for x in (data.get("target_symbols") or []) if x],
            reason=str(data.get("reason") or data.get("reasoning") or ""),
            evidence_needed=[str(x) for x in (data.get("evidence_needed") or []) if x],
            confidence=float(data.get("confidence") or 0),
            stop_condition=str(data.get("stop_condition") or ""),
            risk_flags=[str(x) for x in (data.get("risk_flags") or []) if x],
            tool_name=str(data.get("tool_name") or ""),
            tool_args=dict(data.get("tool_args") or {}),
        )

    @classmethod
    def from_json(cls, text: str) -> PlannerDecision | None:
        raw = (text or "").strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return cls.from_dict(data)
        except json.JSONDecodeError:
            pass
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            try:
                return cls.from_dict(json.loads(m.group(0)))
            except json.JSONDecodeError:
                return None
        return None


def _normalize_action(action: str) -> str:
    a = action.lower().strip()
    aliases = {
        "answer": "final",
        "final_answer": "final",
        "retrieve_more": "read",
        "call_tool": "read",
        "summarize_memory": "summarize",
        "ask_clarification": "ask_user",
        "readsource": "read",
        "strreplace": "edit",
        "write": "edit",
    }
    return aliases.get(a, a)


def tool_to_action(tool: str, *, phase: str = "") -> str:
    t = (tool or "").strip().lower()
    if not t or t == "answer":
        return "final" if phase in ("final_answer", "partial_final_answer", "recovery_final") else "final"
    mapping = {
        "read": "read",
        "readsource": "read",
        "grep": "grep",
        "grepsource": "grep",
        "glob": "glob",
        "globsource": "glob",
        "shell": "shell",
        "write": "edit",
        "strreplace": "edit",
        "edit": "edit",
        "applypatch": "edit",
    }
    return mapping.get(t, "read")
