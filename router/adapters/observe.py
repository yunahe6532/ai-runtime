"""Observe adapter — Langfuse + legacy SSE run events."""

from __future__ import annotations

from typing import Any

from integrations.langfuse import emit_langfuse_event
from legacy import agent_runs as _runs

begin_run = _runs.begin_run
current_run_id = _runs.current_run_id
set_current_run_id = _runs.set_current_run_id
finish_run = _runs.finish_run
get_run = _runs.get_run
list_runs = _runs.list_runs
stream_events_sse = _runs.stream_events_sse
emit_task = _runs.emit_task
emit_tool_call = _runs.emit_tool_call
emit_status = _runs.emit_status
emit_plan_created = _runs.emit_plan_created
emit_evidence_collected = _runs.emit_evidence_collected
update_run_meta = _runs.update_run_meta
link_run_chain = _runs.link_run_chain


def emit_observation(event: str, payload: dict[str, Any]) -> None:
    emit_langfuse_event(event, payload)
    rid = current_run_id()
    if rid:
        emit_task(rid, event, payload)
