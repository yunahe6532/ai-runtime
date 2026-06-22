"""OpenTelemetry flow tracing — replaces JSON-only flow_trace when OTEL enabled."""

from __future__ import annotations

import json
import logging
import os
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

LOG = logging.getLogger("router.integrations.flow_tracing")

FLOW_DIR = Path(os.getenv("CAPTURE_DIR", "/captures"))
FLOW_TRACE_JSON = os.getenv("FLOW_TRACE", os.getenv("CAPTURE_REQUESTS", "0")) == "1"
FLOW_SAVE_BODY = os.getenv("FLOW_SAVE_BODY", "0") == "1"
OTEL_FLOW = os.getenv("OTEL_FLOW_TRACE", "1") == "1"
OTEL_EVENT_CAPTURE = os.getenv("OTEL_EVENT_CAPTURE", "1") == "1"

_active_spans: dict[str, Any] = {}
_active_flows: dict[str, dict[str, Any]] = {}
_recorded_events: list[dict[str, Any]] = []
_turn_root_span: ContextVar[Any | None] = ContextVar("turn_root_span", default=None)
_turn_ctx_token: ContextVar[Any | None] = ContextVar("turn_ctx_token", default=None)


def get_recorded_events() -> list[dict[str, Any]]:
    return list(_recorded_events)


def clear_recorded_events() -> None:
    _recorded_events.clear()


def _attr_value(value: Any) -> str | int | float | bool:
    if isinstance(value, (str, int, float, bool)):
        return value
    if value is None:
        return ""
    return str(value)


def _langfuse_trace_attrs(payload: dict[str, Any]) -> dict[str, str]:
    attrs: dict[str, str] = {}
    flow_id = str(payload.get("flow_id") or "")
    if flow_id:
        attrs["langfuse.trace.metadata.flow_id"] = flow_id
        attrs["langfuse.trace.name"] = f"runtime-turn-{flow_id}"
    for key in ("run_id", "turn_index", "backend", "intent", "phase"):
        val = payload.get(key)
        if val not in (None, ""):
            attrs[f"langfuse.trace.metadata.{key}"] = str(val)
    return attrs


def _payload_attrs(payload: dict[str, Any]) -> dict[str, str | int | float | bool]:
    return {k: _attr_value(v) for k, v in payload.items() if k != "event"}


def _end_turn_root() -> None:
    span = _turn_root_span.get()
    token = _turn_ctx_token.get()
    if span is not None:
        try:
            span.end()
        except Exception:
            pass
    if token is not None:
        try:
            from opentelemetry import trace

            trace.context_api.detach(token)
        except Exception:
            pass
    _turn_root_span.set(None)
    _turn_ctx_token.set(None)


def emit_runtime_event(event: dict[str, Any]) -> None:
    """Record runtime pipeline event → in-memory buffer + OTel span tree."""
    if not event:
        return
    name = str(event.get("event") or "runtime.event")
    payload = {k: _attr_value(v) for k, v in event.items()}

    if OTEL_EVENT_CAPTURE:
        _recorded_events.append(dict(payload))

    tr = _tracer()
    if tr and tr is not False and OTEL_FLOW:
        attrs = _payload_attrs(payload)
        lf_attrs = _langfuse_trace_attrs(payload)
        try:
            from opentelemetry import trace

            if name == "runtime.turn.start":
                _end_turn_root()
                root = tr.start_span(name, attributes={**attrs, **lf_attrs})
                ctx = trace.set_span_in_context(root)
                token = trace.context_api.attach(ctx)
                _turn_root_span.set(root)
                _turn_ctx_token.set(token)
                root.add_event(name, attributes=attrs)
            else:
                root = _turn_root_span.get()
                parent_ctx = None
                if root is not None:
                    parent_ctx = trace.set_span_in_context(root)
                with tr.start_as_current_span(
                    name,
                    context=parent_ctx,
                    attributes={**attrs, **lf_attrs},
                ) as span:
                    span.add_event(name, attributes=attrs)
                if name == "runtime.turn.end":
                    _end_turn_root()
        except Exception as exc:
            LOG.debug("runtime_event otel skip: %s", exc)
            with tr.start_as_current_span(name) as span:
                for key, val in payload.items():
                    if key == "event":
                        continue
                    try:
                        span.set_attribute(key, val)
                    except Exception:
                        span.set_attribute(key, str(val))
                try:
                    span.add_event(name, attributes={k: v for k, v in payload.items() if k != "event"})
                except Exception:
                    pass

    LOG.debug("runtime_event %s", json.dumps(payload, ensure_ascii=False, default=str))


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def _tracer():
    from integrations.otel import _tracer as get_tracer

    return get_tracer()


def _enabled() -> bool:
    return OTEL_FLOW or FLOW_TRACE_JSON


def begin_flow(
    cursor_body: dict[str, Any],
    headers: dict[str, str] | None = None,
    flow_id: str | None = None,
) -> str | None:
    if not _enabled():
        return None
    if not flow_id:
        from capture import _next_id

        flow_id = _next_id()

    messages = cursor_body.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    est = _est_tokens(json.dumps(messages, ensure_ascii=False))

    flow_data = {
        "id": flow_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stages": [{
            "stage": "1_cursor_in",
            "est_tokens": est,
            "message_count": len(messages),
        }],
    }
    _active_flows[flow_id] = flow_data

    tr = _tracer()
    if tr and tr is not False and OTEL_FLOW:
        span = tr.start_span("chat_completion_flow")
        span.set_attribute("flow_id", flow_id)
        span.set_attribute("stage", "cursor_in")
        span.set_attribute("message_count", len(messages))
        span.set_attribute("est_tokens", est)
        span.set_attribute("tool_count", len(cursor_body.get("tools") or []))
        _active_spans[flow_id] = span

    LOG.info("flow begin id=%s msgs=%d est_tokens=%d", flow_id, len(messages), est)
    return flow_id


def record_proxy(
    flow_id: str,
    proxy_body: dict[str, Any],
    *,
    intent: str = "",
    phase: str = "",
    backend: str = "",
    route_reason: str = "",
    raw_tokens: int = 0,
    pack_tokens: int = 0,
    saved_pct: float = 0.0,
) -> None:
    if not _enabled() or not flow_id:
        return

    messages = proxy_body.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    est = _est_tokens(json.dumps(messages, ensure_ascii=False))

    if flow_id in _active_flows:
        _active_flows[flow_id]["stages"].append({
            "stage": "2_router_proxy",
            "intent": intent,
            "phase": phase,
            "backend": backend,
            "raw_tokens": raw_tokens,
            "pack_tokens": pack_tokens,
            "saved_pct": saved_pct,
            "est_tokens": est,
        })

    tr = _tracer()
    if tr and tr is not False and OTEL_FLOW:
        with tr.start_as_current_span("router_proxy") as span:
            span.set_attribute("flow_id", flow_id)
            span.set_attribute("intent", intent)
            span.set_attribute("phase", phase)
            span.set_attribute("backend", backend)
            span.set_attribute("raw_tokens", raw_tokens)
            span.set_attribute("pack_tokens", pack_tokens)
            span.set_attribute("saved_pct", float(saved_pct))
            span.set_attribute("route_reason", route_reason)

    LOG.info(
        "flow proxy id=%s intent=%s phase=%s saved=%.1f%% pack=%d",
        flow_id,
        intent,
        phase,
        saved_pct,
        pack_tokens,
    )


def record_response(
    flow_id: str,
    response: dict[str, Any],
    *,
    elapsed_sec: float = 0.0,
    phase: str = "",
    processed: dict[str, Any] | None = None,
) -> None:
    if not _enabled() or not flow_id:
        return

    try:
        msg = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        msg = {}
    tool_calls = msg.get("tool_calls") or []

    if flow_id in _active_flows:
        flow = _active_flows.pop(flow_id)
        flow["stages"].append({
            "stage": "3_llm_response",
            "elapsed_sec": round(elapsed_sec, 2),
            "phase": phase,
            "tool_calls": len(tool_calls),
        })
        flow["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if FLOW_TRACE_JSON:
            _write_flow_json(flow_id, flow)

    span = _active_spans.pop(flow_id, None)
    if span is not None:
        try:
            span.set_attribute("elapsed_sec", round(elapsed_sec, 2))
            span.set_attribute("phase", phase)
            span.set_attribute("tool_calls", len(tool_calls))
            span.end()
        except Exception:
            pass

    tr = _tracer()
    if tr and tr is not False and OTEL_FLOW:
        with tr.start_as_current_span("llm_response") as s:
            s.set_attribute("flow_id", flow_id)
            s.set_attribute("elapsed_sec", round(elapsed_sec, 2))
            s.set_attribute("phase", phase)
            s.set_attribute("tool_calls", len(tool_calls))

    LOG.info("flow end id=%s elapsed=%.2fs tool_calls=%d", flow_id, elapsed_sec, len(tool_calls))


def _write_flow_json(flow_id: str, flow: dict[str, Any]) -> None:
    try:
        FLOW_DIR.mkdir(parents=True, exist_ok=True)
        path = FLOW_DIR / f"{flow_id}.flow.json"
        path.write_text(json.dumps(flow, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        LOG.warning("flow json save failed: %s", exc)
