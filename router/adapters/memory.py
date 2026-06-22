"""Memory adapter — tiered memory API over legacy store.

Tiers:
  session  — recent dialogue / task state
  artifact — files, tool results
  vector   — retrieval index (via adapters.retrieval)
  policy   — failed actions / preferences
  gpu_hot  — final working set for LLM context
"""

from __future__ import annotations

import json
import os
from typing import Any

from legacy import memory_store as _legacy
from runtime_core.memory_hierarchy import (
    MemoryHierarchySnapshot,
    compute_memory_hierarchy,
    memory_hit_rate,
    repeated_read_avoidance,
)
from runtime_core.memory_policy import MemoryPolicy, WorkingSetPlan

MEMORY_BACKEND = os.getenv("MEMORY_BACKEND", "legacy").strip().lower()
# Back-compat: LANGGRAPH_ENABLED=1 → langgraph backend
if os.getenv("LANGGRAPH_ENABLED", "0") == "1" and MEMORY_BACKEND == "legacy":
    MEMORY_BACKEND = "langgraph"


def _resolve_backend() -> str:
    if MEMORY_BACKEND == "langgraph":
        from integrations.langgraph_memory import is_available, wire_legacy_persistence

        if is_available():
            wire_legacy_persistence()
            return "langgraph"
        LOG = __import__("logging").getLogger("adapters.memory")
        LOG.warning("MEMORY_BACKEND=langgraph but langgraph not installed — falling back to legacy")
    return "legacy"


def memory_backend_name() -> str:
    return _active_backend


def get_memory_backend_metrics() -> dict[str, Any]:
    if _active_backend == "langgraph":
        from integrations.langgraph_memory import get_backend_metrics

        return get_backend_metrics()
    return {"backend": "legacy", "load_latency_ms": 0.0, "save_latency_ms": 0.0}


_active_backend = _resolve_backend()

Artifact = _legacy.Artifact
RequestDelta = _legacy.RequestDelta
SessionState = _legacy.SessionState
extract_delta = _legacy.extract_delta
extract_workspace_path = _legacy.extract_workspace_path
ingest_request = _legacy.ingest_request
normalize_file_path = _legacy.normalize_file_path
project_key_from_workspace = _legacy.project_key_from_workspace
project_paths = _legacy.project_paths


def load_session_state(*args: Any, **kwargs: Any) -> SessionState:
    """Load session memory (alias for load_state)."""
    return load_state(*args, **kwargs)


def load_state(*args: Any, **kwargs: Any) -> SessionState:
    if _active_backend == "langgraph":
        from .langgraph import load_session_state as _lg_load

        return _lg_load(*args, **kwargs)
    return _legacy.load_state(*args, **kwargs)


def save_state(state: SessionState, *args: Any, **kwargs: Any) -> None:
    if _active_backend == "langgraph":
        from .langgraph import save_session_state as _lg_save

        _lg_save(state, *args, **kwargs)
        return
    _legacy.save_state(state, *args, **kwargs)


def save_turn_delta(
    req_id: str,
    body: dict[str, Any],
    state: SessionState | None = None,
) -> tuple[RequestDelta, SessionState]:
    """Persist incremental turn delta into session memory."""
    st = state or load_state()
    delta = extract_delta(req_id, body, st)
    _legacy.update_state_from_delta(st, req_id, body, delta, [], query="")
    save_state(st)
    return delta, st


def save_artifact(
    req_id: str,
    delta: RequestDelta,
    msg: dict[str, Any],
    *,
    state: SessionState,
    messages: list[dict[str, Any]] | None = None,
) -> Artifact | None:
    """Store tool/file result in artifact memory tier."""
    dm = _legacy._delta_message_from_dict(msg, delta.prev_message_count + delta.added_count)
    return _legacy._save_artifact(req_id, delta, msg, dm, messages=messages)


def save_tool_result(
    req_id: str,
    delta: RequestDelta,
    tool_msg: dict[str, Any],
    *,
    state: SessionState,
    messages: list[dict[str, Any]] | None = None,
) -> Artifact | None:
    """Alias for artifact tier ingest of a tool result message."""
    return save_artifact(req_id, delta, tool_msg, state=state, messages=messages)


def query_memory(
    state: SessionState,
    query: str = "",
    *,
    tier: str = "artifact",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Query cold memory tiers without loading GPU context."""
    q = (query or "").lower()
    out: list[dict[str, Any]] = []

    if tier in ("session", "all"):
        out.append(
            {
                "tier": "session",
                "source": "session_state",
                "tokens": len(json.dumps(state.__dict__, default=str)) // 4,
                "meta": {
                    "turn_index": getattr(state, "turn_index", 0),
                    "files_read": len(getattr(state, "files_read", None) or []),
                },
            }
        )

    if tier in ("artifact", "all"):
        for art_id in (getattr(state, "artifacts", None) or [])[:limit]:
            out.append({"tier": "artifact", "source": str(art_id), "tokens": 0})

    if tier in ("policy", "all"):
        failed = getattr(state, "failed_actions", None) or {}
        for action, count in list(failed.items())[:limit]:
            if q and q not in str(action).lower():
                continue
            out.append(
                {
                    "tier": "policy",
                    "source": str(action),
                    "tokens": 0,
                    "meta": {"count": int(count or 0)},
                }
            )
        for row in (getattr(state, "failed_tool_summaries", None) or [])[:limit]:
            out.append({"tier": "policy", "source": "failed_tool", "tokens": 0, "meta": row})

    if tier in ("vector", "all") and q:
        out.append({"tier": "vector", "source": "retrieval_query", "tokens": 0, "meta": {"query": query}})

    return out[:limit]


def compact_memory(state: SessionState, *, max_artifacts: int = 200) -> SessionState:
    """Evict cold artifact references beyond cap (policy tier compaction)."""
    arts = list(getattr(state, "artifacts", None) or [])
    if len(arts) > max_artifacts:
        state.artifacts = arts[-max_artifacts:]
    summaries = list(getattr(state, "failed_tool_summaries", None) or [])
    if len(summaries) > 32:
        state.failed_tool_summaries = summaries[-32:]
    save_state(state)
    return state


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _raw_history_tokens(body: dict[str, Any], state: SessionState) -> int:
    raw = int(getattr(state, "last_raw_tokens", 0) or 0)
    if raw > 0:
        return raw
    messages = body.get("messages") or []
    if isinstance(messages, list):
        return _estimate_tokens(json.dumps(messages, ensure_ascii=False))
    metrics = getattr(state, "last_ingest_metrics", None) or {}
    total_chars = int(metrics.get("messages_total", 0) or 0) * 80
    if total_chars > 0:
        return max(1, total_chars // 4)
    return 0


def _stored_memory_stats(state: SessionState) -> tuple[int, int]:
    items = len(getattr(state, "artifacts", None) or [])
    items += len(getattr(state, "files_read", None) or [])
    items += len(getattr(state, "failed_actions", None) or {})
    tok = _estimate_tokens(json.dumps(getattr(state, "agent_plan", None) or {}, default=str))
    tok += sum(_estimate_tokens(str(a)) for a in (getattr(state, "artifacts", None) or [])[:50])
    return items, tok


def build_working_set(
    *,
    state: SessionState,
    body: dict[str, Any],
    prompt_sources: dict[str, int] | None = None,
    gpu_context_cap: int | None = None,
    policy: MemoryPolicy | None = None,
) -> WorkingSetPlan:
    """Decide GPU working set from prompt source token map."""
    pol = policy or MemoryPolicy(
        gpu_context_cap=gpu_context_cap or int(os.getenv("LONG_CTX_TOKENS", "32768")),
    )
    sources = dict(prompt_sources or getattr(state, "last_prompt_sources", None) or {})
    if not sources:
        sources = {"session_tail": 2048, "retrieved": 4096}
    raw = _raw_history_tokens(body, state)
    return pol.build_working_set(prompt_sources=sources, raw_history_tokens=raw)


def collect_hierarchy_snapshot(
    *,
    state: SessionState,
    body: dict[str, Any],
    retrieval_pack: Any = None,
    coverage: Any = None,
    prompt_pack: Any = None,
    working_set: WorkingSetPlan | None = None,
) -> MemoryHierarchySnapshot:
    """Measure memory funnel for benchmark / Langfuse / inspector."""
    raw = _raw_history_tokens(body, state)
    stored_items, stored_tokens = _stored_memory_stats(state)

    retrieved_tokens = int(getattr(retrieval_pack, "total_tokens", 0) or 0)
    prompt_tokens = 0
    tier_tokens: dict[str, int] = {}

    sources = getattr(prompt_pack, "tokens_used", None) or {}
    tier_tokens: dict[str, int] = {}
    if isinstance(sources, dict) and sources:
        tier_tokens = {str(k): int(v or 0) for k, v in sources.items()}
        prompt_tokens = sum(tier_tokens.values())
    else:
        sources = getattr(prompt_pack, "prompt_sources", None) or getattr(state, "last_prompt_sources", None) or {}
        if isinstance(sources, dict):
            tier_tokens = {str(k): 0 for k in sources.keys()}
        prompt_tokens = 0

    if prompt_tokens <= 0 and prompt_pack is not None:
        used = getattr(prompt_pack, "tokens_used", None) or {}
        if isinstance(used, dict):
            tier_tokens = {str(k): int(v or 0) for k, v in used.items()}
            prompt_tokens = sum(tier_tokens.values())

    ws = working_set or build_working_set(state=state, body=body, prompt_sources=tier_tokens)
    gpu_tokens = int(ws.gpu_context_tokens or prompt_tokens)

    targets = list(getattr(getattr(state, "agent_plan", None) or {}, "coverage_targets", None) or [])
    if not targets and coverage is not None:
        targets = list(getattr(coverage, "missing", None) or [])
    hits = [getattr(i, "source", "") for i in (getattr(retrieval_pack, "items", None) or [])]
    hit_rate = memory_hit_rate(targets=targets, hits=hits) if targets else 1.0
    re_read = repeated_read_avoidance(
        getattr(state, "read_counts", None) or {},
        avoidance_stats=getattr(state, "read_avoidance_stats", None) or {},
    )

    cov_score = float(getattr(coverage, "coverage_score", 0) or 0)
    if not cov_score and getattr(state, "last_runtime_turn", None):
        cov_score = float((state.last_runtime_turn or {}).get("coverage_score", 0) or 0)

    snap = compute_memory_hierarchy(
        raw_history_tokens=raw,
        stored_memory_items=stored_items,
        stored_memory_tokens=stored_tokens,
        retrieved_memory_tokens=retrieved_tokens,
        prompt_pack_tokens=prompt_tokens,
        gpu_context_tokens=gpu_tokens,
        coverage_score=cov_score,
        memory_hit_rate=hit_rate,
        repeated_read_avoidance=re_read,
        tier_tokens=tier_tokens,
    )
    state.last_memory_hierarchy = snap.to_dict()
    state.last_prompt_sources = tier_tokens
    return snap


__all__ = [
    "Artifact",
    "RequestDelta",
    "SessionState",
    "load_session_state",
    "load_state",
    "save_state",
    "save_turn_delta",
    "save_artifact",
    "save_tool_result",
    "query_memory",
    "compact_memory",
    "build_working_set",
    "collect_hierarchy_snapshot",
    "ingest_request",
    "extract_delta",
    "extract_workspace_path",
    "normalize_file_path",
    "project_key_from_workspace",
    "project_paths",
    "MemoryPolicy",
    "WorkingSetPlan",
    "MemoryHierarchySnapshot",
    "MEMORY_BACKEND",
    "memory_backend_name",
    "get_memory_backend_metrics",
]
