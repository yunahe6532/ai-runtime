"""Context budget — static ratios (POC fallback) and dynamic token allocation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from context_need import ContextNeed


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

PRIORITY_TO_SLOT: dict[str, str] = {
    "current_task": "current_task",
    "retrieved": "retrieved",
    "session_tail": "session_tail",
    "long_memory": "session_tail",
    "artifact": "artifact",
    "state": "state",
}

BASE_SLOT_WEIGHTS: dict[str, float] = {
    "system": 0.06,
    "plan": 0.06,
    "state": 0.08,
    "delta": 0.05,
    "session_tail": 0.10,
    "retrieved": 0.10,
    "artifact": 0.10,
    "current_task": 0.15,
}


@dataclass
class RetrievalStats:
    total_tokens: int = 0
    item_count: int = 0
    must_include_tokens: int = 0

    @classmethod
    def from_pack(cls, pack: Any) -> RetrievalStats:
        if pack is None:
            return cls()
        return cls(
            total_tokens=int(getattr(pack, "total_tokens", 0) or 0),
            item_count=len(getattr(pack, "items", None) or []),
            must_include_tokens=sum(
                int(getattr(i, "tokens", 0) or 0)
                for i in (getattr(pack, "items", None) or [])
                if getattr(i, "must_include", False)
            ),
        )


@dataclass
class BudgetPlan:
    """Token ceilings per slot — absolute counts, not ratios."""

    total: int
    system: int
    plan: int
    state: int
    delta: int
    session_tail: int
    retrieved: int
    artifact: int
    current_task: int
    output_reserved: int
    backend: str
    phase: str
    mode: str = "static"

    def to_dict(self) -> dict[str, int | str]:
        return {
            "total": self.total,
            "system": self.system,
            "plan": self.plan,
            "state": self.state,
            "delta": self.delta,
            "session_tail": self.session_tail,
            "retrieved": self.retrieved,
            "artifact": self.artifact,
            "current_task": self.current_task,
            "output_reserved": self.output_reserved,
            "backend": self.backend,
            "phase": self.phase,
            "mode": self.mode,
        }


@dataclass
class ContextBudget:
    """Legacy budget shape for gradual migration."""

    total: int
    system: int
    state: int
    delta: int
    retrieved: int
    session_tail: int
    task: int
    max_output: int
    backend: str
    phase: str

    @classmethod
    def from_plan(cls, plan: BudgetPlan) -> ContextBudget:
        return cls(
            total=plan.total,
            system=plan.system + plan.plan,
            state=plan.state,
            delta=plan.delta,
            retrieved=plan.retrieved,
            session_tail=plan.session_tail + plan.artifact,
            task=plan.current_task,
            max_output=plan.output_reserved,
            backend=plan.backend,
            phase=plan.phase,
        )


def _available_tokens(backend: str, max_output_tokens: int) -> tuple[int, int]:
    max_ctx = BACKEND_CTX_TOKENS.get(backend, BACKEND_CTX_TOKENS["long"])
    output_reserved = max(256, max_output_tokens)
    available = max(4096, max_ctx - output_reserved - CTX_SAFETY_TOKENS)
    return available, output_reserved


def _static_ratios(phase: str) -> dict[str, float]:
    if phase in ("final_answer", "partial_final_answer", "recovery_final"):
        # Evidence-first: most of the input window is for collected synthesis.
        return {
            "system": 0.03,
            "plan": 0.02,
            "state": 0.02,
            "delta": 0.0,
            "retrieved": 0.32,
            "session_tail": 0.32,
            "artifact": 0.18,
            "current_task": 0.08,
        }
    return {
        "system": 0.08,
        "plan": 0.06,
        "state": 0.10,
        "delta": 0.06,
        "retrieved": 0.10,
        "session_tail": 0.10,
        "artifact": 0.10,
        "current_task": 0.40,
    }


def allocate_static(
    backend: str,
    phase: str,
    max_output_tokens: int,
) -> BudgetPlan:
    """POC fixed ratios → absolute token ceilings."""
    available, output_reserved = _available_tokens(backend, max_output_tokens)
    ratios = _static_ratios(phase)
    parts = {k: max(128, int(available * r)) for k, r in ratios.items()}
    used = sum(parts.values())
    if used > available:
        scale = available / used
        parts = {k: max(128, int(v * scale)) for k, v in parts.items()}

    return BudgetPlan(
        total=available,
        system=parts["system"],
        plan=parts["plan"],
        state=parts["state"],
        delta=parts["delta"],
        session_tail=parts["session_tail"],
        retrieved=parts["retrieved"],
        artifact=parts["artifact"],
        current_task=parts["current_task"],
        output_reserved=output_reserved,
        backend=backend,
        phase=phase,
        mode="static",
    )


def allocate_dynamic(
    backend: str,
    phase: str,
    max_output_tokens: int,
    context_need: ContextNeed,
    retrieval_stats: RetrievalStats | None = None,
) -> BudgetPlan:
    """Plan + retrieval-measured tokens → absolute BudgetPlan."""
    available, output_reserved = _available_tokens(backend, max_output_tokens)
    stats = retrieval_stats or RetrievalStats()

    weights = dict(BASE_SLOT_WEIGHTS)
    if phase in ("final_answer", "partial_final_answer", "recovery_final"):
        weights["session_tail"] = max(weights.get("session_tail", 0.1), 0.30)
        weights["retrieved"] = max(weights.get("retrieved", 0.1), 0.30)
        weights["artifact"] = max(weights.get("artifact", 0.1), 0.16)
        weights["plan"] = min(weights.get("plan", 0.06), 0.03)
        weights["state"] = min(weights.get("state", 0.08), 0.03)
        weights["delta"] = 0.0
        weights["current_task"] = max(weights.get("current_task", 0.15), 0.08)

    for pk, pv in (context_need.priority or {}).items():
        slot = PRIORITY_TO_SLOT.get(pk, pk)
        if slot in weights:
            weights[slot] = weights.get(slot, 0.0) + float(pv)

    # Retrieval-first: ensure retrieved slot fits measured content
    if stats.total_tokens > 0:
        min_retrieved = min(stats.total_tokens + 256, int(available * 0.55))
        weights["retrieved"] = max(weights.get("retrieved", 0.1), min_retrieved / max(available, 1))

    total_w = sum(weights.values()) or 1.0
    parts = {k: max(128, int(available * weights[k] / total_w)) for k in weights}

    # must_include floors
    parts["current_task"] = max(parts["current_task"], 400)
    parts["plan"] = max(parts["plan"], 300)
    if phase == "tool_planning":
        parts["artifact"] = max(parts["artifact"], 256)

    if stats.total_tokens > parts["retrieved"]:
        deficit = stats.total_tokens - parts["retrieved"]
        for donor in ("session_tail", "state", "delta", "artifact"):
            if donor not in parts:
                continue
            take = min(deficit, max(0, parts[donor] - 128))
            parts[donor] -= take
            parts["retrieved"] += take
            deficit -= take
            if deficit <= 0:
                break
        parts["retrieved"] = max(parts["retrieved"], min(stats.total_tokens, int(available * 0.5)))

    used = sum(parts.values())
    if used > available:
        scale = available / used
        parts = {k: max(128, int(v * scale)) for k, v in parts.items()}

    return BudgetPlan(
        total=available,
        system=parts["system"],
        plan=parts["plan"],
        state=parts["state"],
        delta=parts["delta"],
        session_tail=parts["session_tail"],
        retrieved=parts["retrieved"],
        artifact=parts["artifact"],
        current_task=parts["current_task"],
        output_reserved=output_reserved,
        backend=backend,
        phase=phase,
        mode="dynamic",
    )


def compute_context_budget(
    backend: str,
    phase: str,
    max_output_tokens: int,
) -> ContextBudget:
    """Backward-compatible static budget."""
    return ContextBudget.from_plan(allocate_static(backend, phase, max_output_tokens))


def truncate_to_token_budget(text: str, budget_tokens: int) -> str:
    if budget_tokens <= 0:
        return ""
    max_chars = budget_tokens * 3
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...(truncated)"
