"""OpenAI-compatible gateway — backend swap without touching runtime policy.

Backends (``GATEWAY_BACKEND`` or ``BACKEND`` env):
  - ``llama_cpp`` — direct httpx to FAST_URL / LONG_URL (default)
  - ``litellm``   — LiteLLM proxy (:4000)
  - ``mock``      — offline tests (benchmark / CI)

``main.py`` must call ``chat_completion()`` only — not engine URLs directly.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

LOG = logging.getLogger("router.adapters.gateway")

FAST_URL = os.getenv("FAST_URL", "http://llama-fast:8081").rstrip("/")
LONG_URL = os.getenv("LONG_URL", "http://llama-long:8082").rstrip("/")
VL_URL = os.getenv("VL_URL", "http://llama-vl:8083").rstrip("/")
LITELLM_URL = os.getenv("LITELLM_URL", "http://127.0.0.1:4000").rstrip("/")
LITELLM_MODEL = os.getenv("LITELLM_MODEL", os.getenv("LITELLM_DEFAULT_MODEL", "gpt-4o-mini"))

HOP_STRIP = frozenset({"host", "content-length"})


def _emit_llm_completed(result: GatewayResult) -> None:
    try:
        from adapters.trace import emit_runtime_event
        from runtime_core.runtime_events import event_llm_completed

        m = result.metrics
        emit_runtime_event(
            event_llm_completed(
                gateway_backend=m.backend,
                latency_ms=m.latency_ms,
                prompt_tokens=m.prompt_tokens,
                completion_tokens=m.completion_tokens,
                status_code=m.status_code,
                error_type=m.error_type or result.error or "",
            )
        )
    except Exception as exc:
        LOG.debug("llm.completed event skip: %s", exc)


def active_backend() -> str:
    raw = os.getenv("GATEWAY_BACKEND", os.getenv("BACKEND", "llama_cpp")).strip().lower()
    if raw in ("llama", "llama_cpp", "llamacpp", "local"):
        return "llama_cpp"
    if raw in ("litellm", "lite_llm"):
        return "litellm"
    if raw == "mock":
        return "mock"
    return raw


def gateway_enabled() -> bool:
    return active_backend() == "litellm"


def llama_engine_url(backend_hint: str) -> str:
    if backend_hint == "fast":
        return FAST_URL
    if backend_hint == "vl":
        return VL_URL
    return LONG_URL


def resolve_engine_url(backend_hint: str) -> str:
    if active_backend() == "litellm":
        return LITELLM_URL
    if active_backend() == "mock":
        return "mock://gateway"
    return llama_engine_url(backend_hint)


def _default_timeout() -> httpx.Timeout:
    read_sec = float(os.getenv("LLM_READ_TIMEOUT_SEC", "120"))
    return httpx.Timeout(connect=30.0, read=read_sec, write=60.0, pool=30.0)


def _clean_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_STRIP}


def _parse_json(content: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(content.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def normalize_completion_response(data: dict[str, Any]) -> dict[str, Any]:
    """Ensure OpenAI-compatible shape: choices · message · usage."""
    out = dict(data)
    choices = out.get("choices")
    if not isinstance(choices, list) or not choices:
        out["choices"] = [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]
    else:
        fixed: list[dict[str, Any]] = []
        for i, ch in enumerate(choices):
            if not isinstance(ch, dict):
                continue
            ch = dict(ch)
            msg = ch.get("message")
            if not isinstance(msg, dict):
                msg = {"role": "assistant", "content": str(ch.get("text") or "")}
            msg = dict(msg)
            msg.setdefault("role", "assistant")
            content = str(msg.get("content") or "")
            if not content.strip() and msg.get("reasoning_content"):
                content = str(msg.get("reasoning_content") or "")
            msg["content"] = content
            if msg.get("tool_calls") is None:
                msg.pop("tool_calls", None)
            ch["message"] = msg
            ch.setdefault("index", i)
            ch.setdefault("finish_reason", ch.get("finish_reason") or "stop")
            fixed.append(ch)
        out["choices"] = fixed or out["choices"]
    usage = out.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    usage.setdefault("prompt_tokens", int(usage.get("prompt_tokens") or 0))
    usage.setdefault("completion_tokens", int(usage.get("completion_tokens") or 0))
    usage.setdefault("total_tokens", int(usage.get("total_tokens") or usage["prompt_tokens"] + usage["completion_tokens"]))
    out["usage"] = usage
    out.setdefault("id", out.get("id") or f"chatcmpl-{uuid.uuid4().hex[:12]}")
    out.setdefault("object", "chat.completion")
    return out


def normalize_error_response(status: int, detail: str, *, error_type: str = "gateway_error") -> dict[str, Any]:
    return {
        "error": {
            "message": detail,
            "type": error_type,
            "code": status,
        }
    }


@dataclass
class GatewayMetrics:
    backend: str
    backend_hint: str
    latency_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    stream: bool = False
    status_code: int = 200
    error_type: str = ""


@dataclass
class GatewayResult:
    status_code: int
    headers: dict[str, str]
    content: bytes
    json_data: dict[str, Any] | None
    metrics: GatewayMetrics
    error: str | None = None


@dataclass
class GatewayStream:
    status_code: int
    headers: dict[str, str]
    metrics: GatewayMetrics
    _iter: Iterator[bytes] = field(repr=False)
    _response: Any = field(default=None, repr=False)

    def iter_bytes(self) -> Iterator[bytes]:
        yield from self._iter

    def close(self) -> None:
        if self._response is not None:
            try:
                self._response.close()
            except Exception:
                pass


def _mock_completion(body_json: dict[str, Any] | None, *, stream: bool) -> GatewayResult | GatewayStream:
    body = body_json or {}
    prompt_toks = max(1, len(json.dumps(body.get("messages", []), ensure_ascii=False)) // 4)
    comp_toks = int(body.get("max_tokens") or 16)
    msg = {
        "role": "assistant",
        "content": "mock gateway ok",
    }
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        msg["tool_calls"] = [
            {
                "id": "call_mock_1",
                "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }
        ]
    payload = normalize_completion_response(
        {
            "choices": [{"message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
            "usage": {
                "prompt_tokens": prompt_toks,
                "completion_tokens": comp_toks,
                "total_tokens": prompt_toks + comp_toks,
            },
        }
    )
    metrics = GatewayMetrics(
        backend="mock",
        backend_hint="long",
        latency_ms=1.0,
        prompt_tokens=prompt_toks,
        completion_tokens=comp_toks,
        total_tokens=prompt_toks + comp_toks,
        stream=stream,
        status_code=200,
    )
    if stream:
        chunk1 = f'data: {json.dumps({"choices":[{"delta":{"content":"mock "}}]})}\n\n'.encode()
        chunk2 = f'data: {json.dumps({"choices":[{"delta":{"content":"gateway"}}]})}\n\n'.encode()
        chunk3 = b"data: [DONE]\n\n"
        return GatewayStream(
            status_code=200,
            headers={"Content-Type": "text/event-stream"},
            metrics=metrics,
            _iter=iter([chunk1, chunk2, chunk3]),
        )
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return GatewayResult(
        status_code=200,
        headers={"Content-Type": "application/json"},
        content=raw,
        json_data=payload,
        metrics=metrics,
    )


def _prepare_litellm_body(body_json: dict[str, Any] | None, body_bytes: bytes | None) -> bytes:
    if body_json is None:
        return body_bytes or b"{}"
    patched = dict(body_json)
    # LiteLLM routes by proxy model_list names — ignore upstream Cursor model ids.
    patched["model"] = LITELLM_MODEL
    return json.dumps(patched, ensure_ascii=False).encode("utf-8")


def _completion_url(backend_hint: str, path: str) -> str:
    backend = active_backend()
    if backend == "litellm":
        if path.startswith("/v1/"):
            return f"{LITELLM_URL}{path}"
        return f"{LITELLM_URL}/v1/chat/completions"
    if backend == "mock":
        return "mock://completion"
    base = llama_engine_url(backend_hint)
    return f"{base}{path}"


def chat_completion(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    body_bytes: bytes | None,
    body_json: dict[str, Any] | None = None,
    backend_hint: str = "long",
    stream: bool = False,
    timeout: httpx.Timeout | None = None,
) -> GatewayResult | GatewayStream:
    """Single entry for non-stream (GatewayResult) or stream (GatewayStream)."""
    backend = active_backend()
    t0 = time.perf_counter()
    hdrs = _clean_headers(headers)

    if backend == "mock":
        result = _mock_completion(body_json, stream=stream)
        if isinstance(result, GatewayResult):
            _emit_llm_completed(result)
        return result

    url = _completion_url(backend_hint, path)
    payload = body_bytes
    if backend == "litellm":
        payload = _prepare_litellm_body(body_json, body_bytes)

    timeout = timeout or _default_timeout()

    if stream:
        try:
            client = httpx.Client(timeout=timeout)
            req = client.build_request(method, url, headers=hdrs, content=payload)
            resp = client.send(req, stream=True)
        except httpx.HTTPError as exc:
            latency = (time.perf_counter() - t0) * 1000.0
            err = normalize_error_response(502, str(exc), error_type="connection_error")
            raw = json.dumps(err).encode()
            return GatewayResult(
                status_code=502,
                headers={"Content-Type": "application/json"},
                content=raw,
                json_data=err,
                metrics=GatewayMetrics(
                    backend=backend,
                    backend_hint=backend_hint,
                    latency_ms=round(latency, 2),
                    stream=True,
                    status_code=502,
                    error_type="connection_error",
                ),
                error=str(exc),
            )
        latency = (time.perf_counter() - t0) * 1000.0
        metrics = GatewayMetrics(
            backend=backend,
            backend_hint=backend_hint,
            latency_ms=round(latency, 2),
            stream=True,
            status_code=resp.status_code,
        )

        def _gen() -> Iterator[bytes]:
            try:
                for chunk in resp.iter_bytes():
                    if chunk:
                        yield chunk
            finally:
                resp.close()
                client.close()

        return GatewayStream(
            status_code=resp.status_code,
            headers=dict(resp.headers),
            metrics=metrics,
            _iter=_gen(),
            _response=resp,
        )

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, url, headers=hdrs, content=payload)
    except httpx.HTTPError as exc:
        latency = (time.perf_counter() - t0) * 1000.0
        err = normalize_error_response(502, str(exc), error_type="connection_error")
        raw = json.dumps(err).encode()
        fail = GatewayResult(
            status_code=502,
            headers={"Content-Type": "application/json"},
            content=raw,
            json_data=err,
            metrics=GatewayMetrics(
                backend=backend,
                backend_hint=backend_hint,
                latency_ms=round(latency, 2),
                stream=False,
                status_code=502,
                error_type="connection_error",
            ),
            error=str(exc),
        )
        _emit_llm_completed(fail)
        return fail

    latency = (time.perf_counter() - t0) * 1000.0
    data = _parse_json(resp.content)
    err_type = ""
    if resp.status_code >= 400:
        err_type = str((data or {}).get("error", {}).get("type") or "http_error")
    elif data:
        data = normalize_completion_response(data)
    usage = (data or {}).get("usage") or {}
    metrics = GatewayMetrics(
        backend=backend,
        backend_hint=backend_hint,
        latency_ms=round(latency, 2),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        stream=False,
        status_code=resp.status_code,
        error_type=err_type,
    )
    content = resp.content
    if data and resp.status_code == 200:
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
    result = GatewayResult(
        status_code=resp.status_code,
        headers=dict(resp.headers),
        content=content,
        json_data=data,
        metrics=metrics,
        error=err_type or None,
    )
    _emit_llm_completed(result)
    return result


def chat_completion_retry(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    body_json: dict[str, Any],
    backend_hint: str = "long",
    timeout: httpx.Timeout | None = None,
) -> dict[str, Any]:
    """Agent retry helper — returns normalized JSON dict."""
    body_bytes = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
    result = chat_completion(
        method=method,
        path=path,
        headers=headers,
        body_bytes=body_bytes,
        body_json=body_json,
        backend_hint=backend_hint,
        stream=False,
        timeout=timeout,
    )
    if not isinstance(result, GatewayResult):
        raise TypeError("chat_completion_retry requires non-stream")
    if result.json_data:
        return result.json_data
    raise RuntimeError(f"gateway retry failed status={result.status_code}")


def forward_completion(
    *,
    url: str,
    headers: dict[str, str],
    body_bytes: bytes | None,
    timeout: httpx.Timeout,
) -> httpx.Response:
    """Deprecated — use chat_completion(). Kept for transitional imports."""
    with httpx.Client(timeout=timeout) as client:
        return client.post(url, headers=headers, content=body_bytes)
