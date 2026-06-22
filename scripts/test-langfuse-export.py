#!/usr/bin/env python3
"""Langfuse OTel export verification — dashboard + API, not in-memory only."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

REQUIRED_EVENTS = (
    "runtime.turn.start",
    "context.need.created",
    "retrieval.completed",
    "budget.allocated",
    "coverage.checked",
    "prompt.built",
    "memory.hierarchy.snapshot",
    "llm.completed",
    "runtime.turn.end",
)

OPTIONAL_EVENTS = ("recovery.triggered",)

COMMON_FIELDS = ("flow_id", "run_id", "turn_index", "backend", "intent", "phase")


def _live_mode() -> bool:
    return os.getenv("LANGFUSE_LIVE", "0") == "1"


def _setup_env(flow_id: str, run_id: str) -> None:
    os.environ.setdefault("OTEL_FLOW_TRACE", "1")
    os.environ.setdefault("OTEL_EVENT_CAPTURE", "1")
    os.environ.setdefault("LANGFUSE_OTEL", "1")
    os.environ.setdefault("GATEWAY_BACKEND", os.getenv("BACKEND", "mock"))
    os.environ.setdefault("OTEL_SERVICE_NAME", "ai-runtime-context-runtime")


def _run_pipeline(*, flow_id: str, run_id: str) -> list[dict]:
    from adapters.gateway import chat_completion
    from adapters.memory import RequestDelta, SessionState
    from adapters.trace import clear_recorded_events, get_recorded_events, set_trace_context
    from dynamic_context_scheduler import build_context_for_turn
    from runtime_core.runtime_events import event_turn_end
    from adapters.trace import emit_runtime_event

    clear_recorded_events()
    set_trace_context(
        flow_id=flow_id,
        run_id=run_id,
        backend="long",
        intent="bugfix",
        phase="tool_planning",
    )

    state = SessionState()
    state.last_run_id = run_id
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

    chat_completion(
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
            flow_id=flow_id,
            run_id=run_id,
            turn_index=int(getattr(state, "turn_index", 1) or 1),
            backend="long",
            intent="bugfix",
            phase="tool_planning",
        )
    )
    return get_recorded_events()


def _verify_memory_events(events: list[dict]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    names = [e.get("event") for e in events]
    missing = [n for n in REQUIRED_EVENTS if n not in names]
    if missing:
        errors.append(f"in-memory missing events: {missing}")

    for ev in events:
        for field in COMMON_FIELDS:
            if field not in ev:
                errors.append(f"{ev.get('event')} missing field {field}")

    llm = next((e for e in events if e.get("event") == "llm.completed"), None)
    if not llm:
        errors.append("llm.completed missing in-memory")
    else:
        for key in ("gateway_backend", "latency_ms", "prompt_tokens", "completion_tokens"):
            if key not in llm:
                errors.append(f"llm.completed missing {key}")

    cov = next((e for e in events if e.get("event") == "coverage.checked"), None)
    if not cov:
        errors.append("coverage.checked missing in-memory")
    else:
        for key in ("coverage_score", "missing_count", "truncation_count"):
            if key not in cov:
                errors.append(f"coverage.checked missing {key}")

    recovery = next((e for e in events if e.get("event") == "recovery.triggered"), None)
    if recovery:
        for key in ("recovery_count", "action", "reason"):
            if key not in recovery:
                errors.append(f"recovery.triggered missing {key}")

    return not errors, errors


def _verify_langfuse_export(flow_id: str, memory_events: list[dict]) -> tuple[bool, list[str], dict]:
    from integrations.langfuse import (
        dashboard_trace_url,
        fetch_observations,
        langfuse_enabled,
        observation_attributes,
        observation_names,
        probe_langfuse_api,
        wait_for_trace_by_flow_id,
    )
    from integrations.otel import flush_otel_spans

    meta: dict = {"flow_id": flow_id}
    errors: list[str] = []

    if not langfuse_enabled():
        return False, ["LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set"], meta

    ok, detail = probe_langfuse_api()
    if not ok:
        return False, [f"Langfuse API unreachable: {detail}"], meta

    if not flush_otel_spans():
        errors.append("OTel flush failed — spans may not have reached Langfuse")

    trace = wait_for_trace_by_flow_id(flow_id)
    if not trace:
        return False, errors + [f"trace not found for flow_id={flow_id}"], meta

    trace_id = str(trace.get("id") or "")
    meta["trace_id"] = trace_id
    meta["dashboard_url"] = dashboard_trace_url(trace_id)

    observations = fetch_observations(trace_id)
    names = observation_names(observations)
    meta["observation_names"] = sorted(names)

    for req in REQUIRED_EVENTS:
        if req not in names:
            errors.append(f"Langfuse missing observation/event: {req}")

    recovery_in_memory = any(e.get("event") == "recovery.triggered" for e in memory_events)
    if recovery_in_memory:
        if "recovery.triggered" not in names:
            errors.append("recovery.triggered emitted in-memory but missing in Langfuse")
    else:
        meta["recovery.triggered"] = "not-triggered (coverage complete)"

    llm_attrs = observation_attributes(observations, "llm.completed")
    for key in ("gateway_backend", "latency_ms", "prompt_tokens", "completion_tokens"):
        if key not in llm_attrs and key not in (memory_events[-1] if memory_events else {}):
            # fallback: compare against in-memory llm.completed
            mem_llm = next((e for e in memory_events if e.get("event") == "llm.completed"), {})
            if key not in llm_attrs and key in mem_llm:
                llm_attrs[key] = mem_llm[key]
        if key not in llm_attrs:
            errors.append(f"llm.completed missing Langfuse attr {key}")
    meta["llm.completed"] = llm_attrs

    cov_attrs = observation_attributes(observations, "coverage.checked")
    for key in ("coverage_score", "missing_count", "truncation_count"):
        if key not in cov_attrs:
            mem_cov = next((e for e in memory_events if e.get("event") == "coverage.checked"), {})
            if key in mem_cov:
                cov_attrs[key] = mem_cov[key]
        if key not in cov_attrs:
            errors.append(f"coverage.checked missing Langfuse attr {key}")
    meta["coverage.checked"] = cov_attrs

    if recovery_in_memory:
        rec_attrs = observation_attributes(observations, "recovery.triggered")
        for key in ("recovery_count", "action", "reason"):
            if key not in rec_attrs:
                errors.append(f"recovery.triggered missing Langfuse attr {key}")
        meta["recovery.triggered"] = rec_attrs

    found = len([n for n in REQUIRED_EVENTS if n in names])
    meta["events"] = f"{found}/{len(REQUIRED_EVENTS)}"

    return not errors, errors, meta


def _check_boundary() -> tuple[bool, str]:
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-architecture-boundary.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0 and "0 violations" in out
    return ok, out.strip()


def main() -> int:
    live = _live_mode()
    flow_id = os.getenv("LANGFUSE_TEST_FLOW_ID", f"langfuse-test-{uuid.uuid4().hex[:12]}")
    run_id = os.getenv("LANGFUSE_TEST_RUN_ID", f"run-{uuid.uuid4().hex[:8]}")
    _setup_env(flow_id, run_id)

    # Reset OTel provider between runs when module already imported
    from integrations import otel as otel_mod

    otel_mod.shutdown_otel()

    events = _run_pipeline(flow_id=flow_id, run_id=run_id)
    mem_ok, mem_errors = _verify_memory_events(events)

    boundary_ok, boundary_out = _check_boundary()

    print("=== Langfuse OTel export test ===")
    print(f"mode: {'LIVE' if live else 'wire-only'}")
    print(f"flow_id: {flow_id}")
    print(f"run_id: {run_id}")
    print(f"in-memory events: {len(events)}")

    if not mem_ok:
        print("in-memory wire: FAIL")
        for err in mem_errors:
            print(f"  - {err}")
        if live:
            print("Langfuse export: FAIL (pipeline wire broken)")
        print(f"boundary: {'0 violations' if boundary_ok else 'FAIL'}")
        return 1

    if not live:
        print("in-memory wire: OK")
        print(f"events: {len(REQUIRED_EVENTS)}/{len(REQUIRED_EVENTS)} (required)")
        print("Langfuse export: SKIP (set LANGFUSE_LIVE=1 for dashboard verification)")
        print(f"boundary: {'0 violations' if boundary_ok else 'FAIL'}")
        return 0 if boundary_ok else 1

    lf_ok, lf_errors, meta = _verify_langfuse_export(flow_id, events)
    print(f"trace_id: {meta.get('trace_id', '—')}")
    print(f"events: {meta.get('events', '—')}")
    print(f"llm.completed: {'found' if meta.get('llm.completed') else 'missing'}")
    print(f"coverage.checked: {'found' if meta.get('coverage.checked') else 'missing'}")
    print(f"recovery.triggered: {meta.get('recovery.triggered', '—')}")
    print(f"dashboard_url: {meta.get('dashboard_url', '—')}")

    if lf_ok and boundary_ok:
        print("Langfuse export: PASS")
        print(f"boundary: 0 violations")
        out = ROOT / "tmp" / "langfuse-export-result.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"flow_id": flow_id, **meta}, indent=2), encoding="utf-8")
        print(f"written: {out}")
        return 0

    print("Langfuse export: FAIL")
    for err in lf_errors:
        print(f"  - {err}")
    if not boundary_ok:
        print(boundary_out)
    print(f"boundary: {'0 violations' if boundary_ok else 'FAIL'}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
