"""Observability layer — trace SSOT, Langfuse/OTel fan-out."""

from __future__ import annotations

from .trace_ssot import (
    TURN_TRACE_FIELDS,
    TraceBackend,
    normalize_turn_trace,
    primary_trace_backend,
    should_emit_langfuse,
    should_persist_turn_log,
)

__all__ = [
    "TURN_TRACE_FIELDS",
    "TraceBackend",
    "normalize_turn_trace",
    "primary_trace_backend",
    "should_emit_langfuse",
    "should_persist_turn_log",
]
