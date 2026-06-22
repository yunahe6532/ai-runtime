"""Tracing adapter — single entry for runtime events + legacy flow stages."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from integrations.flow_tracing import (
    begin_flow,
    clear_recorded_events,
    emit_runtime_event as _emit_runtime_event,
    get_recorded_events,
    record_proxy,
    record_response,
)

_trace_ctx: ContextVar[dict[str, Any]] = ContextVar("trace_ctx", default={})


def set_trace_context(**fields: Any) -> None:
    cur = dict(_trace_ctx.get() or {})
    cur.update({k: v for k, v in fields.items() if v is not None})
    _trace_ctx.set(cur)


def get_trace_context() -> dict[str, Any]:
    return dict(_trace_ctx.get() or {})


def merge_trace_context(event: dict[str, Any]) -> dict[str, Any]:
    ctx = get_trace_context()
    merged = dict(event)
    for key in ("flow_id", "run_id", "turn_index", "backend", "intent", "phase"):
        if not merged.get(key) and ctx.get(key) is not None:
            merged[key] = ctx[key]
    return merged


def emit_runtime_event(event: dict[str, Any]) -> None:
    """Only path from orchestration → OTel (via integrations.flow_tracing)."""
    _emit_runtime_event(merge_trace_context(event))


__all__ = [
    "begin_flow",
    "record_proxy",
    "record_response",
    "emit_runtime_event",
    "set_trace_context",
    "get_trace_context",
    "merge_trace_context",
    "get_recorded_events",
    "clear_recorded_events",
]
