"""Observability — single trace SSOT configuration."""

from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Any

LOG = logging.getLogger("observability.trace_ssot")

# Primary trace store: turn_log (local JSON) | langfuse | otel_only
TRACE_SSOT = os.getenv("TRACE_SSOT", "turn_log").strip().lower()

# Fields every consumer must record (Langfuse / turn_log / inspector alignment)
TURN_TRACE_FIELDS = (
    "turn_id",
    "intent",
    "phase",
    "input_tokens",
    "proxy_tokens",
    "saved_tokens",
    "tool_calls",
    "coverage_score",
    "final_blocked_reason",
    "latency_ms",
    "recovery_triggered",
)


class TraceBackend(str, Enum):
    TURN_LOG = "turn_log"
    LANGFUSE = "langfuse"
    OTEL_ONLY = "otel_only"


def primary_trace_backend() -> TraceBackend:
    raw = TRACE_SSOT
    if raw in ("langfuse", "lf"):
        return TraceBackend.LANGFUSE
    if raw in ("otel", "otel_only"):
        return TraceBackend.OTEL_ONLY
    return TraceBackend.TURN_LOG


def should_emit_langfuse() -> bool:
    return primary_trace_backend() == TraceBackend.LANGFUSE or os.getenv("LANGFUSE_ENABLED", "0") == "1"


def should_persist_turn_log() -> bool:
    return primary_trace_backend() in (TraceBackend.TURN_LOG, TraceBackend.LANGFUSE)


def normalize_turn_trace(payload: dict[str, Any]) -> dict[str, Any]:
    """Project arbitrary turn metrics to SSOT field set."""
    out: dict[str, Any] = {}
    aliases = {
        "turn_id": ("turn_id", "run_id", "flow_id", "req_id"),
        "intent": ("intent", "router_intent"),
        "phase": ("phase", "agent_phase"),
        "input_tokens": ("input_tokens", "raw_tokens"),
        "proxy_tokens": ("proxy_tokens", "pack_tokens"),
        "saved_tokens": ("saved_tokens", "saved_pct"),
        "tool_calls": ("tool_calls", "tool_call_count"),
        "coverage_score": ("coverage_score",),
        "final_blocked_reason": ("final_blocked_reason", "blocked"),
        "latency_ms": ("latency_ms", "total_latency_ms", "llm_latency_ms"),
        "recovery_triggered": ("recovery_triggered", "recovery"),
    }
    for canonical, keys in aliases.items():
        for k in keys:
            if k in payload and payload[k] is not None:
                out[canonical] = payload[k]
                break
    return out
