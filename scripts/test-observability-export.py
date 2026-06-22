#!/usr/bin/env python3
"""Observability export test — 9 runtime events + common fields."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

os.environ.setdefault("OTEL_FLOW_TRACE", "1")
os.environ.setdefault("OTEL_EVENT_CAPTURE", "1")
os.environ.setdefault("GATEWAY_BACKEND", "mock")

REQUIRED_EVENTS = (
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

COMMON_FIELDS = ("flow_id", "run_id", "turn_index", "backend", "intent", "phase")


def test_nine_events_pipeline() -> None:
    from adapters.gateway import chat_completion
    from adapters.memory import RequestDelta, SessionState
    from adapters.trace import clear_recorded_events, get_recorded_events, set_trace_context
    from dynamic_context_scheduler import build_context_for_turn
    from runtime_core.runtime_events import event_turn_end
    from adapters.trace import emit_runtime_event

    clear_recorded_events()
    set_trace_context(
        flow_id="flow-test-1",
        run_id="run-test-1",
        backend="long",
        intent="bugfix",
        phase="tool_planning",
    )

    state = SessionState()
    state.last_run_id = "run-test-1"
    delta = RequestDelta(
        delta_id="d1",
        req_id="r1",
        prev_req_id=None,
        prev_message_count=0,
        curr_message_count=1,
        added_count=1,
    )
    body = {
        "model": "test",
        "messages": [{"role": "user", "content": "fix context_budget.py allocate_dynamic bug"}],
        "max_tokens": 800,
    }

    build_context_for_turn(
        body=body,
        state=state,
        delta=delta,
        artifacts=[],
        intent_name="bugfix",
        phase="tool_planning",
        backend="long",
        index=type("Idx", (), {"query": "fix bug"})(),
        query="fix bug",
    )

    gw = chat_completion(
        method="POST",
        path="/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        body_bytes=json.dumps(body).encode(),
        body_json=body,
        backend_hint="long",
        stream=False,
    )

    emit_runtime_event(
        event_turn_end(
            final_allowed=True,
            final_blocked_reason="",
            total_latency_ms=12.5,
            flow_id="flow-test-1",
            run_id="run-test-1",
            turn_index=int(getattr(state, "turn_index", 1) or 1),
            backend="long",
            intent="bugfix",
            phase="tool_planning",
        )
    )

    events = get_recorded_events()
    names = [e.get("event") for e in events]
    missing = [n for n in REQUIRED_EVENTS if n not in names]
    assert not missing, f"missing events: {missing} got={names}"

    for ev in events:
        for field in COMMON_FIELDS:
            assert field in ev, f"{ev.get('event')} missing {field}"

    llm_events = [e for e in events if e.get("event") == "llm.completed"]
    assert llm_events, "llm.completed not recorded"
    llm = llm_events[-1]
    assert int(llm.get("prompt_tokens") or 0) >= 0
    assert int(llm.get("completion_tokens") or 0) >= 0
    assert float(llm.get("latency_ms") or 0) >= 0
    assert llm.get("gateway_backend") == "mock"
    assert gw is not None


def test_runtime_core_no_otel_imports() -> None:
    import ast
    from pathlib import Path

    core_dir = ROOT / "router" / "runtime_core"
    forbidden = ("adapters", "integrations", "opentelemetry")
    for path in core_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    assert root not in forbidden, f"{path.name} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".", 1)[0]
                assert root not in forbidden, f"{path.name} imports {node.module}"


def main() -> int:
    test_runtime_core_no_otel_imports()
    test_nine_events_pipeline()
    print(f"observability event wire: OK ({len(REQUIRED_EVENTS)} events)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
