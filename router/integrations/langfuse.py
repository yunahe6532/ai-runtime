"""Langfuse integration — HTTP ingest + OTel exporter config + trace verification."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

LOG = logging.getLogger("router.integrations.langfuse")

_HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
_PUBLIC = os.getenv("LANGFUSE_PUBLIC_KEY", "")
_SECRET = os.getenv("LANGFUSE_SECRET_KEY", "")


def _reload_env() -> None:
    global _HOST, _PUBLIC, _SECRET
    _HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    _PUBLIC = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    _SECRET = os.getenv("LANGFUSE_SECRET_KEY", "")


def langfuse_enabled() -> bool:
    _reload_env()
    return bool(_PUBLIC and _SECRET)


def langfuse_otel_exporter_config() -> dict[str, str] | None:
    """Build OTLP HTTP exporter settings for Langfuse ingestion."""
    if not langfuse_enabled():
        return None
    auth = base64.b64encode(f"{_PUBLIC}:{_SECRET}".encode()).decode()
    endpoint = f"{_HOST}/api/public/otel/v1/traces"
    headers = f"Authorization=Basic {auth},x-langfuse-ingestion-version=4"
    return {"endpoint": endpoint, "headers": headers}


def dashboard_trace_url(trace_id: str) -> str:
    _reload_env()
    tid = urllib.parse.quote(str(trace_id), safe="")
    project = os.getenv("LANGFUSE_PROJECT_ID", "proj-ai-runtime")
    return f"{_HOST}/project/{project}/traces/{tid}"


def emit_langfuse_event(name: str, metadata: dict[str, Any] | None = None) -> None:
    if not langfuse_enabled():
        LOG.debug("langfuse_skip event=%s", name)
        return
    try:
        import httpx

        payload = {
            "batch": [
                {
                    "type": "trace-create",
                    "body": {
                        "name": name,
                        "metadata": metadata or {},
                    },
                }
            ]
        }
        auth = (_PUBLIC, _SECRET)
        r = httpx.post(
            f"{_HOST}/api/public/ingestion",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            auth=auth,
            timeout=5.0,
        )
        if r.status_code >= 400:
            LOG.warning("langfuse ingest status=%s", r.status_code)
    except Exception as exc:
        LOG.debug("langfuse error: %s", exc)


def _api_request(path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
    if not langfuse_enabled():
        raise RuntimeError("Langfuse keys not configured")
    query = urllib.parse.urlencode(params or {})
    url = f"{_HOST}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(url, method="GET")
    token = base64.b64encode(f"{_PUBLIC}:{_SECRET}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read().decode("utf-8")
    parsed = json.loads(data)
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def probe_langfuse_api() -> tuple[bool, str]:
    """Return (ok, detail) for Langfuse REST reachability."""
    try:
        _api_request("/api/public/projects")
        return True, "projects API ok"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"auth failed HTTP {exc.code}"
        return False, f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return False, str(exc)


def wait_for_trace_by_flow_id(
    flow_id: str,
    *,
    timeout_sec: float = 45.0,
    poll_sec: float = 2.0,
) -> dict[str, Any] | None:
    """Poll Langfuse traces until metadata.flow_id matches."""
    deadline = time.time() + timeout_sec
    needle = str(flow_id)
    while time.time() < deadline:
        try:
            payload = _api_request("/api/public/traces", params={"limit": "50", "page": "1"})
        except Exception as exc:
            LOG.debug("langfuse trace poll error: %s", exc)
            time.sleep(poll_sec)
            continue
        rows = payload.get("data") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            meta = row.get("metadata") or {}
            if str(meta.get("flow_id") or "") == needle:
                return row
            name = str(row.get("name") or "")
            if needle in name:
                return row
        time.sleep(poll_sec)
    return None


def fetch_observations(trace_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    payload = _api_request(
        "/api/public/observations",
        params={"traceId": str(trace_id), "limit": str(limit)},
    )
    rows = payload.get("data") or []
    return [r for r in rows if isinstance(r, dict)]


def observation_names(observations: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for obs in observations:
        name = str(obs.get("name") or "")
        if name:
            names.add(name)
        meta = obs.get("metadata") or {}
        if isinstance(meta, dict):
            ev = meta.get("event")
            if ev:
                names.add(str(ev))
    return names


def observation_attributes(observations: list[dict[str, Any]], event_name: str) -> dict[str, Any]:
    for obs in observations:
        if str(obs.get("name") or "") != event_name:
            continue
        attrs = obs.get("metadata") or {}
        if isinstance(attrs, dict):
            nested = attrs.get("attributes")
            if isinstance(nested, dict):
                attrs = nested
        out: dict[str, Any] = dict(attrs) if isinstance(attrs, dict) else {}
        for key in (
            "gateway_backend",
            "latency_ms",
            "prompt_tokens",
            "completion_tokens",
            "coverage_score",
            "missing_count",
            "truncation_count",
            "recovery_count",
            "action",
            "reason",
            "flow_id",
            "run_id",
            "turn_index",
        ):
            if key in out:
                continue
            if obs.get(key) is not None:
                out[key] = obs.get(key)
        return out
    return {}
