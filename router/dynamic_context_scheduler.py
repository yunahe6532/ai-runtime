"""Dynamic context scheduler — Memory Scheduler hot path.

Target flow (Local LLM Runtime):
  Need → Project Index → Working Set Plan → Retrieve → Budget → Pre-pack → Prompt → Coverage → Recovery
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from adapters.memory import Artifact, RequestDelta, SessionState, collect_hierarchy_snapshot
from adapters.retrieval import RetrievalPack, retrieve_for_need
from adapters.trace import emit_runtime_event, get_trace_context
from context_budget import (
    BACKEND_CTX_TOKENS,
    BudgetPlan,
    RetrievalStats,
    allocate_dynamic,
    allocate_static,
)
from context_need import extract_context_need
from coverage_checker import check_coverage
from reference.planner import ensure_agent_plan
from recovery_scheduler import BUDGET_BUMP_RATIO, RecoveryScheduler
from runtime_core.runtime_events import (
    event_budget_allocated,
    event_coverage_checked,
    event_memory_hierarchy_snapshot,
    event_need_created,
    event_prompt_built,
    event_recovery_triggered,
    event_retrieval_completed,
    event_turn_start,
)
from runtime_core.scheduler_contract import SchedulerInputs, SchedulerOutputs
from runtime_kernel.project_index import ensure_project_index
from runtime_kernel.task_journal import build_handoff, record_turn_journal
from runtime_kernel.working_set import (
    apply_pre_pack_constraints,
    apply_working_set_to_budget,
    plan_working_set,
)
from runtime_turn_log import record_runtime_turn

LOG = logging.getLogger("router.dynamic_context_scheduler")

DYNAMIC_BUDGET = os.getenv("DYNAMIC_BUDGET", "1") == "1"
COVERAGE_CHECK = os.getenv("COVERAGE_CHECK", "1") == "1"
RECOVERY_ENABLED = os.getenv("RECOVERY_ENABLED", "1") == "1"
PROJECT_INDEX_BOOTSTRAP = os.getenv("PROJECT_INDEX_BOOTSTRAP", "1") == "1"


def _trace_ctx(state: SessionState, *, intent: str, phase: str, backend: str) -> dict[str, Any]:
    ctx = get_trace_context()
    turn_index = int(getattr(state, "turn_index", 0) or 0)
    return {
        "flow_id": ctx.get("flow_id") or getattr(state, "last_run_id", "") or "",
        "run_id": ctx.get("run_id") or getattr(state, "last_run_id", "") or "",
        "turn_index": turn_index,
        "backend": backend or ctx.get("backend") or "",
        "intent": intent or ctx.get("intent") or "",
        "phase": phase or ctx.get("phase") or "",
    }


def _retrieval_backend_label() -> str:
    if os.getenv("LLAMAINDEX_ENABLED", "0") == "1":
        return "llamaindex"
    if os.getenv("VECTOR_RETRIEVAL", "0") == "1":
        return "bm25"
    return "artifact"


def _prompt_token_total(pack: Any) -> int:
    used = getattr(pack, "tokens_used", None) or {}
    if isinstance(used, dict) and used:
        return int(sum(int(v or 0) for v in used.values()))
    try:
        from prompt_builder import estimate_text_tokens

        return int(estimate_text_tokens(getattr(pack, "full_text", "") or ""))
    except Exception:
        return 0


def _record_turn(
    state: SessionState,
    *,
    intent_name: str,
    phase: str,
    need: Any,
    budget: BudgetPlan,
    retrieval_pack: RetrievalPack,
    coverage: Any,
    recovery_triggered: bool = False,
    recovery_recovered: bool = False,
    recovery_rounds: int = 0,
    final_blocked_reason: str = "",
) -> None:
    record_runtime_turn(
        state,
        flow_id=getattr(state, "last_run_id", "") or "",
        intent=intent_name or getattr(need, "intent", ""),
        phase=phase,
        context_need=need,
        budget_plan=budget,
        retrieval_pack=retrieval_pack,
        coverage=coverage,
        recovery_triggered=recovery_triggered,
        recovery_recovered=recovery_recovered,
        recovery_rounds=recovery_rounds,
        final_blocked_reason=final_blocked_reason,
        dynamic_budget_enabled=DYNAMIC_BUDGET,
    )


def build_context_for_turn(
    body: dict[str, Any],
    state: SessionState,
    delta: RequestDelta,
    artifacts: list[Artifact],
    intent_name: str,
    phase: str,
    backend: str,
    index: Any,
    query: str = "",
) -> Any:
    """Memory Scheduler — working set before prompt pack."""
    from context_cache import ContextIndex
    from prompt_builder import (
        TOOL_PLANNING_MAX_TOKENS,
        build_with_budget,
    )

    if not isinstance(index, ContextIndex):
        index = index  # noqa: allow duck-typed index

    query = query or getattr(index, "query", "") or ""
    phase = phase or "tool_planning"
    try:
        from artifact_excerpt import clear_artifact_excerpt_cache

        clear_artifact_excerpt_cache()
    except ImportError:
        pass
    max_out = TOOL_PLANNING_MAX_TOKENS if phase == "tool_planning" else int(
        body.get("max_tokens") or 4096
    )

    state.turn_index = int(getattr(state, "turn_index", 0) or 0) + 1
    tctx = _trace_ctx(state, intent=intent_name, phase=phase, backend=backend)
    emit_runtime_event(event_turn_start(**tctx))

    workspace = getattr(state, "effective_workspace", "") or getattr(state, "workspace_path", "")
    project_index = None
    if PROJECT_INDEX_BOOTSTRAP:
        try:
            project_index = ensure_project_index(state, workspace)
        except Exception as exc:
            LOG.warning("project_index bootstrap skipped: %s", exc)

    agent_plan = ensure_agent_plan(state, query)
    need = extract_context_need(agent_plan, query, intent_name, phase)
    agent_plan.context_need = need.to_dict()
    state.agent_plan = agent_plan.to_dict()
    emit_runtime_event(
        event_need_created(
            need_type=str(getattr(need, "intent", "") or intent_name),
            must_include_count=len(getattr(need, "must_include", None) or []),
            target_count=len(getattr(need, "coverage_targets", None) or []),
            **tctx,
        )
    )

    ws = plan_working_set(
        need, state, backend=backend, phase=phase, max_output=max_out, project_index=project_index,
    )
    state.last_working_set = ws.to_dict()
    try:
        from explorer_trace import write_explorer_trace

        write_explorer_trace(
            "working_set.created",
            phase=phase,
            query=query[:500],
            turn_index=int(getattr(state, "turn_index", 0) or 0),
            result_summary=(
                f"targets={len(ws.priority_targets)} must={len(ws.must_include)} "
                f"retrieved_cap={ws.retrieved_token_cap}"
            ),
            priority_targets=ws.priority_targets[:12],
        )
    except Exception:
        pass

    pre_budget = allocate_static(backend, phase, max_out)
    retrieval_budget = max(ws.retrieved_token_cap, pre_budget.retrieved)

    t_ret0 = time.perf_counter()
    retrieval_pack = retrieve_for_need(
        state, query, delta, need, retrieval_budget, phase=phase,
    )
    emit_runtime_event(
        event_retrieval_completed(
            retrieval_backend=_retrieval_backend_label(),
            retrieved_count=len(getattr(retrieval_pack, "items", None) or []),
            retrieved_tokens=int(getattr(retrieval_pack, "total_tokens", 0) or 0),
            latency_ms=(time.perf_counter() - t_ret0) * 1000.0,
            **tctx,
        )
    )

    stats = RetrievalStats.from_pack(retrieval_pack)
    scheduler_inputs = SchedulerInputs.from_turn(
        intent_name=intent_name,
        phase=phase,
        backend=backend,
        max_output=max_out,
        need=need,
        stats=stats,
    )
    budget = (
        allocate_dynamic(backend, phase, max_out, need, stats)
        if DYNAMIC_BUDGET
        else allocate_static(backend, phase, max_out)
    )
    budget = apply_working_set_to_budget(budget, ws)
    budget = apply_pre_pack_constraints(need, budget, ws)

    scheduler_outputs = SchedulerOutputs.from_budget_plan(budget)
    state.last_scheduler_inputs = scheduler_inputs.to_dict()
    state.last_scheduler_outputs = scheduler_outputs.to_dict()
    ctx_window = BACKEND_CTX_TOKENS.get(backend, BACKEND_CTX_TOKENS["long"])
    emit_runtime_event(
        event_budget_allocated(
            context_window=ctx_window,
            input_budget=int(budget.total),
            output_reserved=int(budget.output_reserved),
            retrieved_budget=int(budget.retrieved),
            session_tail_budget=int(budget.session_tail),
            **tctx,
        )
    )

    pack = build_with_budget(
        body=body,
        state=state,
        delta=delta,
        artifacts=artifacts,
        intent_name=intent_name,
        phase=phase,
        backend=backend,
        index=index,
        query=query,
        budget_plan=budget,
        retrieval_pack=retrieval_pack,
        context_need=need,
    )

    ap = state.agent_plan or {}
    coverage = check_coverage(
        need,
        retrieval_pack,
        pack,
        evidence_needed=list(ap.get("evidence_needed") or []),
        evidence_collected=list(ap.get("evidence_collected") or []),
        source_hits=list(ap.get("source_hits") or []),
        coverage_hits=list(ap.get("coverage_hits") or []),
    )
    pack.coverage = coverage
    try:
        from explorer_trace import write_explorer_trace

        write_explorer_trace(
            "coverage.checked",
            phase=phase,
            query=query[:500],
            turn_index=int(getattr(state, "turn_index", 0) or 0),
            result_summary=(
                f"score={float(getattr(coverage, 'coverage_score', 0) or 0):.2f} "
                f"complete={bool(getattr(coverage, 'complete', False))}"
            ),
            missing=list(getattr(coverage, "missing", None) or [])[:8],
        )
    except Exception:
        pass
    emit_runtime_event(
        event_coverage_checked(
            coverage_score=float(getattr(coverage, "coverage_score", 0) or 0),
            complete=bool(getattr(coverage, "complete", False)),
            missing_count=len(getattr(coverage, "missing", None) or []),
            truncation_count=len(getattr(coverage, "truncated", None) or []),
            **tctx,
        )
    )
    raw_proxy_tokens = int(getattr(state, "last_raw_tokens", 0) or 0)
    prompt_tokens = _prompt_token_total(pack)
    ratio = (prompt_tokens / raw_proxy_tokens) if raw_proxy_tokens > 0 else 0.0
    sources = getattr(pack, "prompt_sources", None) or {}
    prompt_source = ",".join(sorted(sources.keys())[:4]) if isinstance(sources, dict) else "working_set"
    emit_runtime_event(
        event_prompt_built(
            prompt_tokens=prompt_tokens,
            compression_ratio=ratio,
            prompt_source=prompt_source or "working_set",
            **tctx,
        )
    )

    hierarchy = collect_hierarchy_snapshot(
        state=state,
        body=body,
        retrieval_pack=retrieval_pack,
        coverage=coverage,
        prompt_pack=pack,
        working_set=ws.plan,
    )
    emit_runtime_event(
        event_memory_hierarchy_snapshot(
            raw_history_tokens=hierarchy.raw_history_tokens,
            stored_memory_items=hierarchy.stored_memory_items,
            stored_memory_tokens=hierarchy.stored_memory_tokens,
            retrieved_memory_tokens=hierarchy.retrieved_memory_tokens,
            prompt_pack_tokens=hierarchy.prompt_pack_tokens,
            gpu_context_tokens=hierarchy.gpu_context_tokens,
            compression_ratio=hierarchy.compression_ratio,
            memory_hit_rate=hierarchy.memory_hit_rate,
            repeated_read_avoidance=hierarchy.repeated_read_avoidance,
            coverage_score=hierarchy.coverage_score,
            **tctx,
        )
    )

    recovery_triggered = False
    recovery_recovered = False
    recovery_rounds = 0

    pre_tool_reads = (
        phase == "tool_planning"
        and not list(getattr(state, "files_read", None) or [])
        and not list(artifacts or [])
        and not list(getattr(retrieval_pack, "items", None) or [])
    )
    if pre_tool_reads and not coverage.complete:
        LOG.info(
            "recovery_skipped phase=tool_planning reason=awaiting_tool_reads score=%.2f missing=%s",
            float(getattr(coverage, "coverage_score", 0) or 0),
            (getattr(coverage, "missing", None) or [])[:3],
        )
    elif COVERAGE_CHECK and not coverage.complete and RECOVERY_ENABLED:
        recovery_triggered = True
        emit_runtime_event(
            event_recovery_triggered(
                reason=str(getattr(coverage, "action", "") or "coverage_incomplete"),
                recovery_count=0,
                action=str(getattr(coverage, "action", "") or ""),
                budget_bump_ratio=BUDGET_BUMP_RATIO,
                **tctx,
            )
        )
        scheduler = RecoveryScheduler()
        recovery = scheduler.recover(
            context_need=need,
            budget=budget,
            retrieval_pack=retrieval_pack,
            coverage=coverage,
            retrieve_fn=retrieve_for_need,
            build_fn=build_with_budget,
            retrieve_kwargs={
                "state": state,
                "query": query,
                "delta": delta,
                "need": need,
            },
            build_kwargs={
                "body": body,
                "state": state,
                "delta": delta,
                "artifacts": artifacts,
                "intent_name": intent_name,
                "phase": phase,
                "backend": backend,
                "index": index,
                "query": query,
                "context_need": need,
            },
        )
        recovery_rounds = recovery.rounds
        recovery_recovered = recovery.recovered
        if recovery.prompt_pack:
            pack = recovery.prompt_pack
            budget = recovery.budget
            retrieval_pack = recovery.retrieval_pack or retrieval_pack
            coverage = recovery.coverage or coverage
            pack.coverage = coverage

    record_turn_journal(
        state,
        query=query,
        phase=phase,
        intent=intent_name,
        files_read=list(getattr(state, "files_read", None) or []),
    )
    if phase in ("final_answer", "partial_final_answer", "recovery_final"):
        build_handoff(state, query=query)

    try:
        from agent_brain.planner_shadow import run_planner_shadow_if_enabled

        run_planner_shadow_if_enabled(
            state,
            query=query,
            phase=phase,
            router_intent=intent_name,
            context_intent=getattr(need, "intent", "") or intent_name,
            project_index=project_index,
            working_set=ws,
            budget_plan=budget,
            coverage=coverage,
            context_need=need,
        )
    except Exception as exc:
        LOG.warning("planner_shadow hook failed: %s", exc)

    LOG.info(
        "turn_summary phase=%s intent=%s coverage=%.2f complete=%s ws_targets=%d pack_tokens=%d",
        phase,
        intent_name,
        float(getattr(coverage, "coverage_score", 0) or 0),
        bool(getattr(coverage, "complete", False)),
        len(ws.priority_targets),
        _prompt_token_total(pack),
    )

    _record_turn(
        state,
        intent_name=intent_name,
        phase=phase,
        need=need,
        budget=budget,
        retrieval_pack=retrieval_pack,
        coverage=coverage,
        recovery_triggered=recovery_triggered,
        recovery_recovered=recovery_recovered,
        recovery_rounds=recovery_rounds,
        final_blocked_reason="",
    )

    final_blocked_reason = ""
    if (
        not coverage.complete
        or coverage.critical_source_truncated
        or coverage.latest_tool_result_missing
    ):
        from reference.loop_guard import should_block_final_answer

        blocked, reason = should_block_final_answer(
            state,
            can_final=True,
            task_intent=str(ap.get("task_intent") or ""),
            intent_name=intent_name,
        )
        if blocked:
            final_blocked_reason = reason

    if final_blocked_reason and getattr(state, "last_runtime_turn", None):
        state.last_runtime_turn["final_blocked_reason"] = final_blocked_reason
        metrics = dict(getattr(state, "last_ingest_metrics", None) or {})
        metrics["final_blocked_reason"] = final_blocked_reason
        state.last_ingest_metrics = metrics

    if recovery_triggered:
        LOG.info(
            "dynamic_context recovery rounds=%d recovered=%s score=%.2f action=%s",
            recovery_rounds,
            recovery_recovered,
            coverage.coverage_score,
            coverage.action,
        )

    return pack
