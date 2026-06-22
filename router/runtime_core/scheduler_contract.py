"""Scheduler contract — explicit inputs/outputs for Context Runtime orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from context_budget import BACKEND_CTX_TOKENS, BudgetPlan, RetrievalStats
from context_need import ContextNeed


@dataclass
class SchedulerInputs:
    """What the Context Scheduler reads each turn."""

    intent: str
    phase: str
    retrieved_tokens: int = 0
    coverage_score: float = 1.0
    coverage_complete: bool = True
    gpu_backend: str = "long"
    context_window: int = 0
    max_output_tokens: int = 4096
    recovery_round: int = 0
    context_need: ContextNeed | None = None
    retrieval_stats: RetrievalStats | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "phase": self.phase,
            "retrieved_tokens": self.retrieved_tokens,
            "coverage_score": self.coverage_score,
            "coverage_complete": self.coverage_complete,
            "gpu_backend": self.gpu_backend,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "recovery_round": self.recovery_round,
        }

    @classmethod
    def from_turn(
        cls,
        *,
        intent_name: str,
        phase: str,
        backend: str,
        max_output: int,
        need: ContextNeed,
        stats: RetrievalStats | None = None,
        coverage: Any = None,
        recovery_round: int = 0,
    ) -> SchedulerInputs:
        ctx = BACKEND_CTX_TOKENS.get(backend, BACKEND_CTX_TOKENS["long"])
        cov_score = float(getattr(coverage, "coverage_score", 1.0) or 1.0)
        return cls(
            intent=intent_name or getattr(need, "intent", ""),
            phase=phase or "tool_planning",
            retrieved_tokens=int(getattr(stats, "total_tokens", 0) or 0),
            coverage_score=cov_score,
            coverage_complete=bool(getattr(coverage, "complete", True)),
            gpu_backend=backend,
            context_window=ctx,
            max_output_tokens=max_output,
            recovery_round=recovery_round,
            context_need=need,
            retrieval_stats=stats,
        )


@dataclass
class SchedulerOutputs:
    """Token budget per slot — absolute counts allocated for this turn."""

    history: int = 0  # session_tail + delta
    retrieved: int = 0
    artifact: int = 0
    memory: int = 0  # long_memory / state slot
    output_tokens: int = 0
    system: int = 0
    plan: int = 0
    current_task: int = 0
    total: int = 0
    mode: str = "dynamic"
    budget_plan: BudgetPlan | None = None

    @classmethod
    def from_budget_plan(cls, plan: BudgetPlan) -> SchedulerOutputs:
        history = int(plan.session_tail + plan.delta)
        memory = int(plan.state)
        return cls(
            history=history,
            retrieved=int(plan.retrieved),
            artifact=int(plan.artifact),
            memory=memory,
            output_tokens=int(plan.output_reserved),
            system=int(plan.system),
            plan=int(plan.plan),
            current_task=int(plan.current_task),
            total=int(plan.total),
            mode=str(plan.mode),
            budget_plan=plan,
        )

    def to_dict(self) -> dict[str, int | str]:
        return {
            "history": self.history,
            "retrieved": self.retrieved,
            "artifact": self.artifact,
            "memory": self.memory,
            "output_tokens": self.output_tokens,
            "system": self.system,
            "plan": self.plan,
            "current_task": self.current_task,
            "total": self.total,
            "mode": self.mode,
        }


@dataclass
class SchedulerTurnResult:
    """Full scheduler output for one turn (budget + pack metadata)."""

    inputs: SchedulerInputs
    outputs: SchedulerOutputs
    extras: dict[str, Any] = field(default_factory=dict)
