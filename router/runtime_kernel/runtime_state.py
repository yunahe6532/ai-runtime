"""Runtime Kernel — turn state JSON for AI Planner consumption."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .intent import RuntimeIntentResolution
from .phase import RuntimePhase


@dataclass
class RuntimeState:
    """Single JSON-shaped view of one turn — Kernel output, Brain input."""

    turn_id: str = ""
    flow_id: str = ""
    query: str = ""
    phase: str = "tool_planning"
    intent: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, Any] = field(default_factory=dict)
    self_model_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RuntimeState:
        if not data:
            return cls()
        fields = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**fields)


def build_runtime_state(
    *,
    turn_id: str = "",
    flow_id: str = "",
    query: str = "",
    phase: str | RuntimePhase = "tool_planning",
    intent: RuntimeIntentResolution | None = None,
    session_state: Any | None = None,
    budget_plan: Any | None = None,
    coverage: Any | None = None,
    self_model_excerpt: str = "",
) -> RuntimeState:
    """Assemble RuntimeState from kernel artifacts + session."""
    phase_name = phase.value if isinstance(phase, RuntimePhase) else RuntimePhase.normalize(phase).value
    ap = getattr(session_state, "agent_plan", None) or {} if session_state else {}

    budget: dict[str, Any] = {}
    if budget_plan is not None:
        if hasattr(budget_plan, "to_dict"):
            budget = budget_plan.to_dict()
        elif isinstance(budget_plan, dict):
            budget = dict(budget_plan)

    memory = {
        "artifact_count": len(getattr(session_state, "artifacts", None) or []) if session_state else 0,
        "files_read_count": len(getattr(session_state, "files_read", None) or []) if session_state else 0,
        "turn_index": int(getattr(session_state, "turn_index", 0) or 0) if session_state else 0,
        "raw_tokens": int(getattr(session_state, "last_raw_tokens", 0) or 0) if session_state else 0,
        "workspace": getattr(session_state, "workspace_path", "") or "" if session_state else "",
    }

    evidence = {
        "needed": list(ap.get("evidence_needed") or []),
        "collected": list(ap.get("evidence_collected") or []),
        "source_hits": list(ap.get("source_hits") or []),
        "coverage_hits": list(ap.get("coverage_hits") or []),
        "final_ready": bool(ap.get("final_ready")),
    }

    cov: dict[str, Any] = {}
    if coverage is not None:
        if hasattr(coverage, "to_dict"):
            cov = coverage.to_dict()
        elif isinstance(coverage, dict):
            cov = dict(coverage)

    tools = {
        "allowed": list(ap.get("allowed_tools") or []),
        "banned": list(ap.get("banned_tools") or []),
        "next_action": dict(ap.get("next_action") or {}),
    }

    return RuntimeState(
        turn_id=turn_id,
        flow_id=flow_id,
        query=query,
        phase=phase_name,
        intent=intent.to_dict() if intent else {},
        budget=budget,
        memory=memory,
        evidence=evidence,
        coverage=cov,
        tools=tools,
        self_model_excerpt=self_model_excerpt,
    )


def persist_runtime_state(session_state: Any, runtime_state: RuntimeState) -> None:
    """Store on session for Agent Brain / observability."""
    if session_state is None:
        return
    session_state.runtime_state = runtime_state.to_dict()
    ap = dict(getattr(session_state, "agent_plan", None) or {})
    if runtime_state.intent:
        ap["runtime_intent"] = runtime_state.intent
        ap["task_intent"] = runtime_state.intent.get("evidence_profile", ap.get("task_intent", "general"))
        ap["router_intent"] = runtime_state.intent.get("name", ap.get("router_intent", ""))
    session_state.agent_plan = ap
