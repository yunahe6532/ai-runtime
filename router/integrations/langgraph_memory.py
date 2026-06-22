"""LangGraph checkpointer + store memory backend (Buy).

All LangGraph imports live here only. ``adapters.memory`` delegates when
``MEMORY_BACKEND=langgraph``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from legacy.memory_store import (
    CACHE_DIR,
    SessionState,
    project_key_from_workspace,
)

LOG = logging.getLogger("integrations.langgraph_memory")

_lock = threading.Lock()
_last_load_latency_ms: float = 0.0
_last_save_latency_ms: float = 0.0

_STORE = None
_STORE_CM = None
_CHECKPOINTER = None
_CHECKPOINTER_CM = None


def _langgraph_root() -> Path:
    root = Path(os.getenv("LANGGRAPH_MEMORY_DIR", str(CACHE_DIR / "langgraph")))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _store_db_path() -> Path:
    return _langgraph_root() / "store.db"


def _checkpoint_db_path() -> Path:
    return _langgraph_root() / "checkpoints.db"


def _namespace(project_key: str | None) -> tuple[str, str]:
    pk = project_key or "default"
    if pk == "unknown":
        pk = "default"
    return ("memory", pk)


def _session_fields() -> set[str]:
    return {f.name for f in fields(SessionState)}


def _state_from_dict(data: dict[str, Any]) -> SessionState:
    names = _session_fields()
    return SessionState(**{k: data[k] for k in names if k in data})


def _get_store():
    global _STORE, _STORE_CM
    if _STORE is not None:
        return _STORE
    try:
        from langgraph.store.sqlite import SqliteStore

        _STORE_CM = SqliteStore.from_conn_string(str(_store_db_path()))
        _STORE = _STORE_CM.__enter__()
        LOG.info("langgraph store sqlite=%s", _store_db_path())
    except ImportError:
        from langgraph.store.memory import InMemoryStore

        _STORE = InMemoryStore()
        LOG.warning("langgraph store fallback=InMemoryStore (install langgraph for sqlite)")
    except Exception as exc:
        LOG.warning("langgraph sqlite store failed (%s) — InMemoryStore", exc)
        from langgraph.store.memory import InMemoryStore

        _STORE = InMemoryStore()
    return _STORE


def _get_checkpointer():
    global _CHECKPOINTER, _CHECKPOINTER_CM
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        _CHECKPOINTER_CM = SqliteSaver.from_conn_string(str(_checkpoint_db_path()))
        _CHECKPOINTER = _CHECKPOINTER_CM.__enter__()
        LOG.info("langgraph checkpointer sqlite=%s", _checkpoint_db_path())
    except ImportError:
        from langgraph.checkpoint.memory import MemorySaver

        _CHECKPOINTER = MemorySaver()
        LOG.warning("langgraph checkpointer fallback=MemorySaver")
    except Exception as exc:
        LOG.warning("langgraph sqlite checkpointer failed (%s) — MemorySaver", exc)
        from langgraph.checkpoint.memory import MemorySaver

        _CHECKPOINTER = MemorySaver()
    return _CHECKPOINTER


def last_load_latency_ms() -> float:
    return round(_last_load_latency_ms, 3)


def last_save_latency_ms() -> float:
    return round(_last_save_latency_ms, 3)


def load_state(project_key: str | None = None) -> SessionState:
    """Load SessionState from LangGraph store."""
    global _last_load_latency_ms
    t0 = time.perf_counter()
    store = _get_store()
    ns = _namespace(project_key)
    try:
        item = store.get(ns, "session_state")
    except Exception as exc:
        LOG.warning("langgraph store get failed ns=%s: %s", ns, exc)
        item = None
    if item and getattr(item, "value", None):
        state = _state_from_dict(dict(item.value))
        _last_load_latency_ms = (time.perf_counter() - t0) * 1000.0
        return state
    seed = project_key or uuid.uuid4().hex[:12]
    from capture import _sha256

    state = SessionState(session_id=_sha256(str(seed))[:12], project_key=project_key or "")
    _last_load_latency_ms = (time.perf_counter() - t0) * 1000.0
    return state


def save_state(state: SessionState, project_key: str | None = None) -> None:
    """Persist SessionState to LangGraph store + checkpoint turn."""
    global _last_save_latency_ms
    t0 = time.perf_counter()
    pk = project_key or state.project_key or None
    ns = _namespace(pk)
    payload = asdict(state)
    store = _get_store()
    store.put(ns, "session_state", payload)

    cp = _get_checkpointer()
    thread_id = ns[1]
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns[0]}}
    try:
        from langgraph.checkpoint.base import empty_checkpoint

        ckpt = empty_checkpoint()
        ckpt["id"] = str(uuid.uuid4())
        ckpt["channel_values"] = {
            "session_id": state.session_id,
            "turn_index": int(getattr(state, "turn_index", 0) or 0),
            "artifact_count": len(getattr(state, "artifacts", None) or []),
        }
        meta = {
            "source": "memory_backend",
            "step": int(getattr(state, "turn_index", 0) or 0),
            "writes": {"session_state": "updated"},
        }
        cp.put(config, ckpt, meta, {})
    except Exception as exc:
        LOG.debug("langgraph checkpoint put skipped: %s", exc)

    _last_save_latency_ms = (time.perf_counter() - t0) * 1000.0


def put_artifact_meta(artifact_id: str, payload: dict[str, Any], *, project_key: str | None = None) -> None:
    """Optional artifact index in LangGraph store (raw bytes stay on disk)."""
    store = _get_store()
    ns = _namespace(project_key)
    store.put(ns, f"artifact:{artifact_id}", payload)


def get_backend_metrics() -> dict[str, Any]:
    return {
        "backend": "langgraph",
        "load_latency_ms": last_load_latency_ms(),
        "save_latency_ms": last_save_latency_ms(),
        "store_path": str(_store_db_path()),
        "checkpoint_path": str(_checkpoint_db_path()),
    }


def wire_legacy_persistence() -> None:
    """Route legacy.memory_store load/save through LangGraph backend."""
    import legacy.memory_store as ms

    ms.load_state = load_state
    ms.save_state = save_state


def is_available() -> bool:
    try:
        import langgraph  # noqa: F401

        return True
    except ImportError:
        return False
