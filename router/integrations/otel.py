"""OpenTelemetry integration — OTLP export (Langfuse when LANGFUSE_* is set)."""

from __future__ import annotations

import logging
import os
from typing import Any

LOG = logging.getLogger("router.integrations.otel")

_TRACER = None
_PROVIDER = None
_CONFIGURED = False


def _langfuse_export_requested() -> bool:
    if os.getenv("LANGFUSE_OTEL", "0") == "1":
        return True
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def _enabled() -> bool:
    return bool(
        os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or _langfuse_export_requested()
        or os.getenv("OTEL_SERVICE_NAME")
        or os.getenv("OTEL_FLOW_TRACE", "0") == "1"
    )


def _configure_provider() -> Any:
    global _PROVIDER, _CONFIGURED, _TRACER
    if _CONFIGURED:
        return _TRACER

    if not _enabled():
        _TRACER = None
        _CONFIGURED = True
        return _TRACER

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        from integrations.langfuse import langfuse_otel_exporter_config

        service = os.getenv("OTEL_SERVICE_NAME", "ai-runtime-context-runtime")
        provider = TracerProvider(resource=Resource.create({"service.name": service}))

        endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT"
        )
        headers_raw = os.getenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS") or os.getenv(
            "OTEL_EXPORTER_OTLP_HEADERS"
        )
        if not endpoint and _langfuse_export_requested():
            lf = langfuse_otel_exporter_config()
            if lf:
                endpoint = lf["endpoint"]
                headers_raw = lf["headers"]

        if endpoint:
            headers = _parse_otlp_headers(headers_raw)
            exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers or None)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            LOG.info("otel exporter configured endpoint=%s", endpoint)
        else:
            LOG.debug("otel tracer enabled without exporter endpoint")

        trace.set_tracer_provider(provider)
        _PROVIDER = provider
        _TRACER = trace.get_tracer("ai-runtime.context")
    except ImportError:
        LOG.info(
            "opentelemetry not installed — pip install -r router/requirements-integrations.txt"
        )
        _TRACER = False
    except Exception as exc:
        LOG.warning("otel configure failed: %s", exc)
        _TRACER = False

    _CONFIGURED = True
    return _TRACER


def _parse_otlp_headers(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, val = part.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def _tracer():
    if not _CONFIGURED:
        _configure_provider()
    return _TRACER


def flush_otel_spans(*, timeout_ms: int | None = None) -> bool:
    """Force-flush pending OTLP spans (required before Langfuse API verification)."""
    if not _CONFIGURED:
        _configure_provider()
    if _PROVIDER is None:
        return False
    wait = timeout_ms
    if wait is None:
        wait = int(os.getenv("OTEL_EXPORTER_FLUSH_TIMEOUT_MS", "10000"))
    try:
        return bool(_PROVIDER.force_flush(timeout_millis=wait))
    except Exception as exc:
        LOG.warning("otel flush failed: %s", exc)
        return False


def shutdown_otel() -> None:
    global _PROVIDER, _TRACER, _CONFIGURED
    if _PROVIDER is not None:
        try:
            _PROVIDER.shutdown()
        except Exception:
            pass
    _PROVIDER = None
    _TRACER = None
    _CONFIGURED = False


def emit_turn_span(turn: dict[str, Any], *, service: str = "ai-runtime") -> None:
    if not turn:
        return
    tr = _tracer()
    if not tr or tr is False:
        LOG.debug(
            "otel_skip flow_id=%s coverage=%.2f",
            turn.get("flow_id"),
            float(turn.get("coverage_score", 0) or 0),
        )
        return
    with tr.start_as_current_span("runtime_turn") as span:
        span.set_attribute("flow_id", str(turn.get("flow_id", "")))
        span.set_attribute("intent", str(turn.get("intent", "")))
        span.set_attribute("phase", str(turn.get("phase", "")))
        span.set_attribute("coverage_score", float(turn.get("coverage_score", 0) or 0))
        span.set_attribute("coverage_complete", bool(turn.get("coverage_complete")))
        span.set_attribute("recovery_triggered", bool(turn.get("recovery_triggered")))
        span.set_attribute("recovery_recovered", bool(turn.get("recovery_recovered")))
        span.set_attribute("retrieval_tokens", int(turn.get("retrieval_total_tokens", 0) or 0))
        if turn.get("final_blocked_reason"):
            span.set_attribute("final_blocked_reason", str(turn["final_blocked_reason"]))
