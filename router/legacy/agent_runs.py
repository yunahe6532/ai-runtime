"""Cursor SDK-aligned agent run store + SSE event stream.

Maps router planner/executor lifecycle to normalized SDKMessage-style events.
See https://cursor.com/docs/sdk/typescript — Stream events (SDKMessage).

Efficiency notes from official SDK:
- Stable envelope: type, agent_id, run_id, call_id, name, status
- Unstable payloads: tool_call args/result treated as opaque summaries
- task events for plan/milestone summaries (not raw CoT in chat completions)
- thinking events for short planner summaries only
- Dual access: snapshot GET + live SSE (like run.stream + run.wait)
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import queue
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

LOG = logging.getLogger("router.agent_runs")

_CAPTURE_BASE = Path(os.getenv("CAPTURE_DIR", "/captures"))
RUNS_DIR = Path(os.getenv("AGENT_RUNS_DIR", str(_CAPTURE_BASE / "agent-runs")))
ENABLED = os.getenv("AGENT_RUNS_ENABLED", "1") == "1"
MAX_EVENTS = int(os.getenv("AGENT_RUNS_MAX_EVENTS", "500"))
MAX_RUNS = int(os.getenv("AGENT_RUNS_MAX_RUNS", "200"))
SSE_HEARTBEAT_SEC = float(os.getenv("AGENT_RUNS_SSE_HEARTBEAT_SEC", "15"))
RUN_TTL_INTERACTIVE_SEC = int(os.getenv("RUN_TTL_INTERACTIVE", "120"))
RUN_TTL_ANALYSIS_SEC = int(os.getenv("RUN_TTL_ANALYSIS", "300"))
ANALYSIS_INTENTS = frozenset({"explain", "log_analysis", "project_inspection", "debug", "continue_previous"})

_current_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("agent_run_id", default=None)

_lock = threading.RLock()
_runs: dict[str, "AgentRun"] = {}
_subscribers: dict[str, list[queue.Queue[dict[str, Any]]]] = {}


@dataclass
class AgentRun:
    agent_id: str
    run_id: str
    status: str = "running"  # running | finished | partial | error
    query: str = ""
    intent: str = ""
    phase: str = ""
    backend: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    flow_id: str = ""
    conversation_id: str = ""
    parent_run_id: str = ""
    turn_index: int = 0
    error: str = ""
    result_preview: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0

    def to_meta(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "status": self.status,
            "query": self.query[:400],
            "intent": self.intent,
            "phase": self.phase,
            "backend": self.backend,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "flow_id": self.flow_id,
            "conversation_id": self.conversation_id,
            "parent_run_id": self.parent_run_id,
            "turn_index": self.turn_index,
            "event_count": len(self.events),
            "error": self.error,
            "result_preview": self.result_preview[:300],
            "supports": {
                "stream": True,
                "wait": self.status != "running",
                "cancel": False,
                "conversation": self.status != "running",
                "partial": self.status == "partial",
            },
        }


def set_current_run_id(run_id: str | None) -> None:
    _current_run_id.set(run_id)


def current_run_id() -> str | None:
    return _current_run_id.get()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_agent_id() -> str:
    return f"local-{secrets.token_hex(6)}"


def _run_ttl_sec(run: AgentRun) -> int:
    if run.intent in ANALYSIS_INTENTS:
        return RUN_TTL_ANALYSIS_SEC
    return RUN_TTL_INTERACTIVE_SEC


def _started_epoch(iso: str) -> float:
    try:
        return time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0.0


def expire_stale_runs() -> int:
    """Mark long-running runs as error (disconnect/timeout). Returns count expired."""
    if not ENABLED:
        return 0
    now = time.time()
    expired = 0
    with _lock:
        for run in list(_runs.values()):
            if run.status != "running":
                continue
            started = _started_epoch(run.started_at)
            if not started:
                continue
            if now - started <= _run_ttl_sec(run):
                continue
            run.status = "error"
            run.error = "timeout_or_disconnect"
            run.finished_at = _now_iso()
            run.duration_ms = int((now - started) * 1000)
            expired += 1
            _persist_run(run.run_id)
    return expired


def begin_run(
    run_id: str,
    *,
    query: str = "",
    agent_id: str | None = None,
    flow_id: str = "",
) -> str | None:
    if not ENABLED:
        return None
    expire_stale_runs()
    aid = agent_id or _new_agent_id()
    with _lock:
        if run_id in _runs:
            return run_id
        run = AgentRun(
            agent_id=aid,
            run_id=run_id,
            query=query,
            flow_id=flow_id or run_id,
            started_at=_now_iso(),
        )
        _runs[run_id] = run
        _trim_runs()
    emit_system(run_id, subtype="init", extra={"runtime": "local", "harness": "cursor-local-llm-router"})
    if query:
        emit_user(run_id, query)
    emit_status(run_id, "running", "run started")
    _persist_run(run_id)
    return run_id


def finish_run(
    run_id: str,
    *,
    status: str = "finished",
    phase: str = "",
    intent: str = "",
    backend: str = "",
    result_preview: str = "",
    error: str = "",
) -> None:
    if not ENABLED or not run_id:
        return
    with _lock:
        run = _runs.get(run_id)
        if not run:
            return
        run.status = status
        run.phase = phase or run.phase
        run.intent = intent or run.intent
        run.backend = backend or run.backend
        run.result_preview = result_preview
        run.error = error
        run.finished_at = _now_iso()
        try:
            t0 = time.strptime(run.started_at, "%Y-%m-%dT%H:%M:%SZ")
            t1 = time.strptime(run.finished_at, "%Y-%m-%dT%H:%M:%SZ")
            run.duration_ms = int((time.mktime(t1) - time.mktime(t0)) * 1000)
        except (ValueError, TypeError):
            run.duration_ms = 0
    emit_status(run_id, status, error or "run completed")
    _persist_run(run_id)
    _notify_subscribers(run_id, None)


def get_run(run_id: str) -> AgentRun | None:
    with _lock:
        if run_id in _runs:
            return _runs[run_id]
    return _load_run(run_id)


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        items = sorted(_runs.values(), key=lambda r: r.started_at, reverse=True)
    if len(items) < limit:
        _load_recent_from_disk(limit)
        with _lock:
            items = sorted(_runs.values(), key=lambda r: r.started_at, reverse=True)
    return [r.to_meta() for r in items[:limit]]


def _base_event(run_id: str, event_type: str, **fields: Any) -> dict[str, Any]:
    with _lock:
        run = _runs.get(run_id)
        if not run:
            return {}
        run._seq += 1
        seq = run._seq
        agent_id = run.agent_id
    ev: dict[str, Any] = {
        "type": event_type,
        "agent_id": agent_id,
        "run_id": run_id,
        "seq": seq,
        "at": _now_iso(),
        **fields,
    }
    with _lock:
        run = _runs.get(run_id)
        if run:
            run.events.append(ev)
            if len(run.events) > MAX_EVENTS:
                run.events = run.events[-MAX_EVENTS:]
    _notify_subscribers(run_id, ev)
    return ev


def emit_system(run_id: str, *, subtype: str = "init", extra: dict[str, Any] | None = None) -> None:
    if not ENABLED or not run_id:
        return
    payload: dict[str, Any] = {"subtype": subtype}
    if extra:
        payload.update(extra)
    _base_event(run_id, "system", **payload)


def emit_user(run_id: str, text: str) -> None:
    if not ENABLED or not run_id:
        return
    _base_event(
        run_id,
        "user",
        message={"role": "user", "content": [{"type": "text", "text": text[:2000]}]},
    )


def emit_thinking(run_id: str, text: str, *, duration_ms: int | None = None) -> None:
    """Planner/progress summary — not raw model CoT."""
    if not ENABLED or not run_id or not text.strip():
        return
    fields: dict[str, Any] = {"text": text[:1200]}
    if duration_ms is not None:
        fields["thinking_duration_ms"] = duration_ms
    _base_event(run_id, "thinking", **fields)


def emit_task(run_id: str, status: str, text: str = "", *, data: dict[str, Any] | None = None) -> None:
    """SDK task milestone — plan.created, evidence.collected, final.ready."""
    if not ENABLED or not run_id:
        return
    fields: dict[str, Any] = {"status": status, "text": text[:800]}
    if data:
        fields["data"] = data
    _base_event(run_id, "task", **fields)


def emit_status(run_id: str, status: str, message: str = "") -> None:
    if not ENABLED or not run_id:
        return
    _base_event(run_id, "status", status=status, message=message[:400])


def emit_tool_call(
    run_id: str,
    *,
    call_id: str,
    name: str,
    status: str,
    args: dict[str, Any] | None = None,
    result: Any = None,
    truncated: bool = False,
    guard_reason: str = "",
) -> None:
    if not ENABLED or not run_id:
        return
    fields: dict[str, Any] = {
        "call_id": call_id,
        "name": name,
        "status": status,
    }
    if args is not None:
        fields["args"] = _summarize_args(args)
    if result is not None:
        fields["result"] = _summarize_result(result)
    if truncated:
        fields["truncated"] = True
    if guard_reason:
        fields["guard_reason"] = guard_reason[:200]
    _base_event(run_id, "tool_call", **fields)


def emit_plan_created(run_id: str, plan: dict[str, Any]) -> None:
    if not run_id:
        return
    summary = (
        f"intent={plan.get('task_intent', '?')} "
        f"next={((plan.get('next_action') or {}).get('tool')) or '?'} "
        f"known_files={len(plan.get('known_files') or [])}"
    )
    emit_task(run_id, "plan.created", summary)
    avoid = plan.get("avoid_actions") or []
    if avoid:
        emit_thinking(run_id, "Avoid: " + "; ".join(str(a) for a in avoid[:4]))
    na = plan.get("next_action") or {}
    if na:
        emit_thinking(
            run_id,
            f"Next: {na.get('tool', '?')} {na.get('target', '')} — {na.get('reason', '')}",
        )


def update_run_meta(run_id: str, **fields: Any) -> None:
    if not ENABLED or not run_id:
        return
    with _lock:
        run = _runs.get(run_id)
        if not run:
            return
        for key, val in fields.items():
            if hasattr(run, key):
                setattr(run, key, val)
    _persist_run(run_id)


def link_run_chain(run_id: str, state: Any) -> None:
    """Attach multi-turn chain metadata (conversation_id, parent_run_id, turn_index)."""
    if not ENABLED or not run_id:
        return
    with _lock:
        run = _runs.get(run_id)
        if not run:
            return
        conv = getattr(state, "chat_id", "") or getattr(state, "session_id", "") or ""
        run.conversation_id = conv
        run.parent_run_id = getattr(state, "last_run_id", "") or ""
        run.turn_index = int(getattr(state, "turn_index", 0) or 0) + 1
        state.turn_index = run.turn_index
        state.last_run_id = run_id
    _persist_run(run_id)


def emit_evidence_collected(
    run_id: str,
    tags: list[str],
    *,
    source: str = "",
    target: str = "",
) -> None:
    if not tags:
        return
    payload = {
        "evidence": tags,
        "source": source,
        "target": target[:240],
    }
    emit_task(
        run_id,
        "evidence.collected",
        ", ".join(tags[:8]),
        data=payload,
    )


def emit_from_current_run(fn, *args, **kwargs) -> None:
    rid = current_run_id()
    if rid:
        fn(rid, *args, **kwargs)


def subscribe(run_id: str) -> queue.Queue[dict[str, Any]]:
    q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
    with _lock:
        _subscribers.setdefault(run_id, []).append(q)
    return q


def unsubscribe(run_id: str, q: queue.Queue[dict[str, Any]]) -> None:
    with _lock:
        subs = _subscribers.get(run_id, [])
        if q in subs:
            subs.remove(q)


def stream_events_sse(run_id: str, *, after_seq: int = 0) -> Iterator[str]:
    run = get_run(run_id)
    if not run:
        yield f"event: error\ndata: {json.dumps({'message': 'run not found'})}\n\n"
        return

    last = after_seq
    for ev in run.events:
        if int(ev.get("seq", 0)) > last:
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            last = int(ev["seq"])

    if run.status != "running":
        yield f"event: done\ndata: {json.dumps({'status': run.status})}\n\n"
        return

    q = subscribe(run_id)
    try:
        while True:
            with _lock:
                run = _runs.get(run_id)
                if not run:
                    break
                if run.status != "running":
                    for ev in run.events:
                        if int(ev.get("seq", 0)) > last:
                            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                            last = int(ev["seq"])
                    yield f"event: done\ndata: {json.dumps({'status': run.status})}\n\n"
                    break
            try:
                ev = q.get(timeout=SSE_HEARTBEAT_SEC)
                if ev is None:
                    yield ": heartbeat\n\n"
                    continue
                if int(ev.get("seq", 0)) > last:
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    last = int(ev["seq"])
            except queue.Empty:
                yield ": heartbeat\n\n"
    finally:
        unsubscribe(run_id, q)


def _notify_subscribers(run_id: str, ev: dict[str, Any] | None) -> None:
    with _lock:
        subs = list(_subscribers.get(run_id, []))
    for q in subs:
        try:
            q.put_nowait(ev or {"type": "status", "status": "closed"})
        except queue.Full:
            pass


def _summarize_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in args.items():
        s = str(v)
        out[k] = s[:240] + ("…" if len(s) > 240 else "")
    return out


def _summarize_result(result: Any) -> str:
    s = str(result)
    return s[:400] + ("…" if len(s) > 400 else "")


def _trim_runs() -> None:
    if len(_runs) <= MAX_RUNS:
        return
    ordered = sorted(_runs.values(), key=lambda r: r.started_at)
    for old in ordered[: len(_runs) - MAX_RUNS]:
        _runs.pop(old.run_id, None)


def _persist_run(run_id: str) -> None:
    try:
        with _lock:
            run = _runs.get(run_id)
            if not run:
                return
            payload = {**run.to_meta(), "events": run.events}
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        path = RUNS_DIR / f"{run_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        LOG.warning("agent_run persist failed: %s", exc)


def _load_run(run_id: str) -> AgentRun | None:
    path = RUNS_DIR / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        run = AgentRun(
            agent_id=data.get("agent_id", _new_agent_id()),
            run_id=run_id,
            status=data.get("status", "finished"),
            query=data.get("query", ""),
            intent=data.get("intent", ""),
            phase=data.get("phase", ""),
            backend=data.get("backend", ""),
            started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at", ""),
            duration_ms=int(data.get("duration_ms") or 0),
            flow_id=data.get("flow_id", run_id),
            conversation_id=data.get("conversation_id", ""),
            parent_run_id=data.get("parent_run_id", ""),
            turn_index=int(data.get("turn_index") or 0),
            error=data.get("error", ""),
            result_preview=data.get("result_preview", ""),
            events=list(data.get("events") or []),
            _seq=max((int(e.get("seq", 0)) for e in data.get("events") or []), default=0),
        )
        with _lock:
            _runs[run_id] = run
        return run
    except Exception as exc:
        LOG.warning("agent_run load failed %s: %s", run_id, exc)
        return None


def _load_recent_from_disk(limit: int) -> None:
    try:
        if not RUNS_DIR.exists():
            return
        paths = sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in paths[:limit]:
            _load_run(p.stem)
    except Exception:
        pass
