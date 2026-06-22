"""Working Set Planner — hot-path selection before retrieve/prompt pack.

Runtime Memory → Working Set → Prompt Pack → GPU Context (KV prefix cache optional).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from context_budget import BACKEND_CTX_TOKENS, BudgetPlan
from context_need import ContextNeed
from runtime_core.memory_policy import MemoryPolicy, WorkingSetPlan

LOG = logging.getLogger("runtime_kernel.working_set")


@dataclass
class TurnWorkingSet:
    """Per-turn plan: what enters GPU context this turn."""

    priority_targets: list[str] = field(default_factory=list)
    must_include: list[str] = field(default_factory=list)
    retrieved_token_cap: int = 2048
    session_tail_cap: int = 1024
    artifact_cap: int = 2048
    slot_caps: dict[str, int] = field(default_factory=dict)
    project_index_used: bool = False
    plan: WorkingSetPlan | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "priority_targets": list(self.priority_targets),
            "must_include": list(self.must_include),
            "retrieved_token_cap": self.retrieved_token_cap,
            "session_tail_cap": self.session_tail_cap,
            "artifact_cap": self.artifact_cap,
            "slot_caps": dict(self.slot_caps),
            "project_index_used": self.project_index_used,
        }
        if self.plan:
            out["working_set_plan"] = self.plan.to_dict()
        return out


def plan_working_set(
    need: ContextNeed,
    state: Any,
    *,
    backend: str,
    phase: str,
    max_output: int,
    project_index: Any | None = None,
) -> TurnWorkingSet:
    """Decide working set BEFORE retrieve — core Memory Scheduler step."""
    ctx = BACKEND_CTX_TOKENS.get(backend, BACKEND_CTX_TOKENS["long"])
    gpu_cap = max(4096, ctx - max_output - 2000)
    policy = MemoryPolicy(gpu_context_cap=gpu_cap)

    targets = list(getattr(need, "coverage_targets", None) or [])[:12]
    must = list(getattr(need, "must_include", None) or [])
    priority = dict(getattr(need, "priority", None) or {})

    retrieved_ratio = float(priority.get("retrieved", 0.2))
    session_ratio = float(priority.get("session_tail", 0.15))
    artifact_ratio = float(priority.get("artifact", 0.15))

    if phase in ("final_answer", "partial_final_answer", "recovery_final"):
        retrieved_ratio = max(retrieved_ratio, 0.45)
        session_ratio = max(session_ratio, 0.25)
        artifact_ratio = max(artifact_ratio, 0.2)

    retrieved_cap = max(512, int(gpu_cap * retrieved_ratio))
    session_cap = max(256, int(gpu_cap * session_ratio))
    artifact_cap = max(256, int(gpu_cap * artifact_ratio))

    if project_index and getattr(project_index, "entrypoints", None):
        for ep in project_index.entrypoints[:4]:
            if ep not in targets:
                targets.append(ep)
    elif isinstance(project_index, dict):
        for ep in (project_index.get("entrypoints") or [])[:4]:
            if ep not in targets:
                targets.append(ep)

    slot_caps = {
        "retrieved": retrieved_cap,
        "session_tail": session_cap,
        "artifact": artifact_cap,
        "current_task": max(256, int(gpu_cap * float(priority.get("current_task", 0.12)))),
    }

    prompt_sources = {k: v for k, v in slot_caps.items() if v > 0}
    raw_tokens = int(getattr(state, "last_raw_tokens", 0) or 0)
    ws_plan = policy.build_working_set(prompt_sources=prompt_sources, raw_history_tokens=raw_tokens)

    tws = TurnWorkingSet(
        priority_targets=targets,
        must_include=must,
        retrieved_token_cap=retrieved_cap,
        session_tail_cap=session_cap,
        artifact_cap=artifact_cap,
        slot_caps=slot_caps,
        project_index_used=bool(project_index),
        plan=ws_plan,
    )
    LOG.info(
        "working_set_plan phase=%s targets=%d retrieved_cap=%d gpu_cap=%d",
        phase, len(targets), retrieved_cap, gpu_cap,
    )
    return tws


def apply_working_set_to_budget(budget: BudgetPlan, ws: TurnWorkingSet) -> BudgetPlan:
    """Clamp BudgetPlan slots to working set caps."""
    caps = ws.slot_caps or {}
    return BudgetPlan(
        total=budget.total,
        system=budget.system,
        plan=budget.plan,
        state=budget.state,
        delta=min(budget.delta, caps.get("delta", budget.delta)),
        session_tail=min(budget.session_tail, ws.session_tail_cap),
        retrieved=min(budget.retrieved, ws.retrieved_token_cap),
        artifact=min(budget.artifact, ws.artifact_cap),
        current_task=min(budget.current_task, caps.get("current_task", budget.current_task)),
        output_reserved=budget.output_reserved,
        backend=budget.backend,
        phase=budget.phase,
        mode=budget.mode + "+working_set",
    )


def apply_pre_pack_constraints(
    need: ContextNeed,
    budget: BudgetPlan,
    ws: TurnWorkingSet,
) -> BudgetPlan:
    """Pre-pack coverage: reserve budget for must_include before prompt build."""
    must = list(getattr(need, "must_include", None) or ws.must_include or [])
    if not must:
        return budget
    reserve = min(512 * len(must), max(256, budget.current_task // 2))
    current_task = max(256, budget.current_task)
    if "latest tool result" in [m.lower() for m in must]:
        artifact = max(budget.artifact, min(ws.artifact_cap, 1024))
        return BudgetPlan(
            total=budget.total,
            system=budget.system,
            plan=budget.plan,
            state=budget.state,
            delta=budget.delta,
            session_tail=budget.session_tail,
            retrieved=budget.retrieved,
            artifact=artifact,
            current_task=current_task,
            output_reserved=budget.output_reserved,
            backend=budget.backend,
            phase=budget.phase,
            mode=budget.mode + "+pre_pack",
        )
    return BudgetPlan(
        total=budget.total,
        system=budget.system,
        plan=budget.plan,
        state=budget.state,
        delta=budget.delta,
        session_tail=budget.session_tail,
        retrieved=budget.retrieved,
        artifact=budget.artifact,
        current_task=max(current_task, reserve),
        output_reserved=budget.output_reserved,
        backend=budget.backend,
        phase=budget.phase,
        mode=budget.mode + "+pre_pack",
    )
