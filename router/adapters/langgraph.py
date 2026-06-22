"""LangGraph persistence adapter — delegates to integrations.langgraph_memory."""

from __future__ import annotations

from typing import Any

from legacy import memory_store as _legacy
from integrations.langgraph_memory import (
    get_backend_metrics,
    is_available,
    last_load_latency_ms,
    last_save_latency_ms,
    load_state as _lg_load_state,
    save_state as _lg_save_state,
)


def load_session_state(*args: Any, **kwargs: Any) -> _legacy.SessionState:
    if not is_available():
        return _legacy.load_state(*args, **kwargs)
    return _lg_load_state(*args, **kwargs)


def save_session_state(state: _legacy.SessionState, *args: Any, **kwargs: Any) -> None:
    if not is_available():
        _legacy.save_state(state, *args, **kwargs)
        return
    _lg_save_state(state, *args, **kwargs)


load_state = load_session_state
save_state = save_session_state

__all__ = [
    "load_session_state",
    "save_session_state",
    "load_state",
    "save_state",
    "get_backend_metrics",
    "last_load_latency_ms",
    "last_save_latency_ms",
    "is_available",
]
