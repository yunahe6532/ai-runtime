"""Runtime observability events — pure dict builders (no OTel / no adapters)."""

from __future__ import annotations

from typing import Any

RUNTIME_EVENT_NAMES: tuple[str, ...] = (
    "runtime.turn.start",
    "context.need.created",
    "retrieval.completed",
    "budget.allocated",
    "coverage.checked",
    "recovery.triggered",
    "prompt.built",
    "memory.hierarchy.snapshot",
    "llm.completed",
    "runtime.turn.end",
)


def base_fields(
    *,
    flow_id: str = "",
    run_id: str = "",
    turn_index: int = 0,
    backend: str = "",
    intent: str = "",
    phase: str = "",
) -> dict[str, Any]:
    return {
        "flow_id": flow_id,
        "run_id": run_id,
        "turn_index": int(turn_index or 0),
        "backend": backend,
        "intent": intent,
        "phase": phase,
    }


def event_turn_start(**ctx: Any) -> dict[str, Any]:
    return {**base_fields(**ctx), "event": "runtime.turn.start"}


def event_need_created(
    *,
    need_type: str = "",
    must_include_count: int = 0,
    target_count: int = 0,
    **ctx: Any,
) -> dict[str, Any]:
    return {
        **base_fields(**ctx),
        "event": "context.need.created",
        "need_type": need_type,
        "must_include_count": int(must_include_count),
        "target_count": int(target_count),
    }


def event_retrieval_completed(
    *,
    retrieval_backend: str = "",
    retrieved_count: int = 0,
    retrieved_tokens: int = 0,
    latency_ms: float = 0.0,
    **ctx: Any,
) -> dict[str, Any]:
    return {
        **base_fields(**ctx),
        "event": "retrieval.completed",
        "retrieval_backend": retrieval_backend,
        "retrieved_count": int(retrieved_count),
        "retrieved_tokens": int(retrieved_tokens),
        "latency_ms": round(float(latency_ms), 2),
    }


def event_budget_allocated(
    *,
    context_window: int = 0,
    input_budget: int = 0,
    output_reserved: int = 0,
    retrieved_budget: int = 0,
    session_tail_budget: int = 0,
    **ctx: Any,
) -> dict[str, Any]:
    return {
        **base_fields(**ctx),
        "event": "budget.allocated",
        "context_window": int(context_window),
        "input_budget": int(input_budget),
        "output_reserved": int(output_reserved),
        "retrieved_budget": int(retrieved_budget),
        "session_tail_budget": int(session_tail_budget),
    }


def event_coverage_checked(
    *,
    coverage_score: float = 0.0,
    complete: bool = False,
    missing_count: int = 0,
    truncation_count: int = 0,
    **ctx: Any,
) -> dict[str, Any]:
    return {
        **base_fields(**ctx),
        "event": "coverage.checked",
        "coverage_score": round(float(coverage_score), 4),
        "complete": bool(complete),
        "missing_count": int(missing_count),
        "truncation_count": int(truncation_count),
    }


def event_recovery_triggered(
    *,
    reason: str = "",
    recovery_count: int = 0,
    action: str = "",
    budget_bump_ratio: float = 1.0,
    **ctx: Any,
) -> dict[str, Any]:
    return {
        **base_fields(**ctx),
        "event": "recovery.triggered",
        "reason": reason,
        "recovery_count": int(recovery_count),
        "action": action,
        "budget_bump_ratio": float(budget_bump_ratio),
    }


def event_prompt_built(
    *,
    prompt_tokens: int = 0,
    compression_ratio: float = 0.0,
    prompt_source: str = "",
    **ctx: Any,
) -> dict[str, Any]:
    return {
        **base_fields(**ctx),
        "event": "prompt.built",
        "prompt_tokens": int(prompt_tokens),
        "compression_ratio": round(float(compression_ratio), 4),
        "prompt_source": prompt_source,
    }


def event_memory_hierarchy_snapshot(
    *,
    raw_history_tokens: int = 0,
    stored_memory_items: int = 0,
    stored_memory_tokens: int = 0,
    retrieved_memory_tokens: int = 0,
    prompt_pack_tokens: int = 0,
    gpu_context_tokens: int = 0,
    compression_ratio: float = 0.0,
    memory_hit_rate: float = 0.0,
    repeated_read_avoidance: float = 0.0,
    coverage_score: float = 0.0,
    **ctx: Any,
) -> dict[str, Any]:
    return {
        **base_fields(**ctx),
        "event": "memory.hierarchy.snapshot",
        "raw_history_tokens": int(raw_history_tokens),
        "stored_memory_items": int(stored_memory_items),
        "stored_memory_tokens": int(stored_memory_tokens),
        "retrieved_memory_tokens": int(retrieved_memory_tokens),
        "prompt_pack_tokens": int(prompt_pack_tokens),
        "gpu_context_tokens": int(gpu_context_tokens),
        "compression_ratio": round(float(compression_ratio), 4),
        "memory_hit_rate": round(float(memory_hit_rate), 4),
        "repeated_read_avoidance": round(float(repeated_read_avoidance), 4),
        "coverage_score": round(float(coverage_score), 4),
    }


def event_llm_completed(
    *,
    gateway_backend: str = "",
    latency_ms: float = 0.0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    status_code: int = 200,
    error_type: str = "",
    **ctx: Any,
) -> dict[str, Any]:
    return {
        **base_fields(**ctx),
        "event": "llm.completed",
        "gateway_backend": gateway_backend,
        "latency_ms": round(float(latency_ms), 2),
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "status_code": int(status_code),
        "error_type": error_type or "",
    }


def event_turn_end(
    *,
    final_allowed: bool = True,
    final_blocked_reason: str = "",
    total_latency_ms: float = 0.0,
    **ctx: Any,
) -> dict[str, Any]:
    return {
        **base_fields(**ctx),
        "event": "runtime.turn.end",
        "final_allowed": bool(final_allowed),
        "final_blocked_reason": final_blocked_reason or "",
        "total_latency_ms": round(float(total_latency_ms), 2),
    }
