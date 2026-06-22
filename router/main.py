#!/usr/bin/env python3
"""Exclusive fast/long llama-server router for Cursor."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from capture import maybe_capture, _next_id
from adapters.gateway import GatewayResult, GatewayStream, chat_completion, chat_completion_retry
from adapters.trace import begin_flow, record_proxy, record_response, set_trace_context
from adapters.observe import (
    begin_run,
    current_run_id,
    finish_run,
    get_run,
    list_runs,
    set_current_run_id,
    stream_events_sse,
)
from reference.agent_exec import (
    completion_json_to_sse,
    finalize_client_response,
    is_exec_intent,
    normalize_client_response,
    postprocess_agent_response,
    sanitize_agent_response,
)
from context_cache import extract_last_user_query
from intent_router import process_two_pass
from vl_pass import (
    last_user_message_has_image,
    VL_PASS_ENABLED,
    count_images,
    has_real_image_content,
    maybe_vl_preprocess,
    normalize_messages_for_coder,
    normalize_messages_for_multimodal,
)

LOG = logging.getLogger("router")

ROUTER_HOST = os.getenv("ROUTER_HOST", "0.0.0.0")
ROUTER_PORT = int(os.getenv("ROUTER_PORT", "8080"))
TWO_PASS_ROUTER = os.getenv("TWO_PASS_ROUTER", "1") == "1"
FAST_URL = os.getenv("FAST_URL", "http://llama-fast:8081").rstrip("/")
LONG_URL = os.getenv("LONG_URL", "http://llama-long:8082").rstrip("/")
VL_URL = os.getenv("VL_URL", "http://llama-vl:8083").rstrip("/")
ROUTER_MODE = os.getenv("ROUTER_MODE", "legacy").strip().lower()
_mm_raw = os.getenv("MULTIMODAL_BACKEND", "").strip().lower()
MULTIMODAL_BACKEND = _mm_raw == "1" if _mm_raw else ROUTER_MODE == "unified"
FAST_CONTAINER = os.getenv("FAST_CONTAINER", "cursor-local-llm-fast")
LONG_CONTAINER = os.getenv("LONG_CONTAINER", "cursor-local-llm-long")
VL_CONTAINER = os.getenv("VL_CONTAINER", "cursor-local-llm-vl")
BACKEND_CONTAINERS: dict[str, str] = {
    "fast": FAST_CONTAINER,
    "long": LONG_CONTAINER,
    "vl": VL_CONTAINER,
}
TOKEN_THRESHOLD = int(os.getenv("TOKEN_THRESHOLD", "20000"))
LONG_IDLE_TTL_SEC = int(os.getenv("LONG_IDLE_TTL_SEC", "900"))
ROUTER_EXCLUSIVE = os.getenv("ROUTER_EXCLUSIVE", "1") == "1"
DEFAULT_BACKEND = os.getenv("DEFAULT_BACKEND", "fast")
READY_TIMEOUT_SEC = int(os.getenv("BACKEND_READY_TIMEOUT_SEC", "300"))
DOCKER_SOCK = os.getenv("DOCKER_SOCK", "/var/run/docker.sock")
DOCKER_API = os.getenv("DOCKER_API_VERSION", "v1.44")
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
SSE_DONE_MARKER = b"data: [DONE]"
ALLOWED_PATH_PREFIXES = ("/v1/", "/router/")


class RouterState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.active = DEFAULT_BACKEND if DEFAULT_BACKEND in ("fast", "long") else "fast"
        self.last_activity = time.time()
        self.last_switch_sec: float | None = None
        self.switch_count = 0
        # Prevents other threads from switching away from VL while VL inference is in flight.
        self.vl_pass_active = threading.Event()

    def touch(self) -> None:
        self.last_activity = time.time()

    def long_sticky(self) -> bool:
        if self.active != "long":
            return False
        return (time.time() - self.last_activity) < LONG_IDLE_TTL_SEC


STATE = RouterState()
DOCKER = httpx.Client(
    transport=httpx.HTTPTransport(uds=DOCKER_SOCK),
    base_url="http://docker",
    timeout=httpx.Timeout(90.0, connect=10.0),
)


def backend_url(name: str) -> str:
    if name == "fast":
        return FAST_URL
    if name == "vl":
        return VL_URL
    return LONG_URL


def backend_container(name: str) -> str:
    return BACKEND_CONTAINERS.get(name, LONG_CONTAINER)


def docker_inspect(name: str) -> dict[str, Any] | None:
    r = DOCKER.get(f"/{DOCKER_API}/containers/{name}/json")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def docker_running(name: str) -> bool:
    try:
        data = docker_inspect(name)
        return bool(data and data.get("State", {}).get("Running"))
    except Exception as exc:
        LOG.warning("docker inspect failed for %s: %s", name, exc)
        return False


def docker_stop(name: str) -> None:
    if not docker_running(name):
        return
    LOG.info("stopping %s", name)
    r = DOCKER.post(f"/{DOCKER_API}/containers/{name}/stop", params={"t": 20})
    if r.status_code not in (204, 304):
        LOG.warning("stop %s: HTTP %s %s", name, r.status_code, r.text[:200])


def docker_start(name: str) -> None:
    if docker_running(name):
        return
    LOG.info("starting %s", name)
    r = DOCKER.post(f"/{DOCKER_API}/containers/{name}/start")
    if r.status_code in (204, 304):
        return
    if r.status_code == 400 and "already started" in r.text.lower():
        return
    raise RuntimeError(f"start {name} failed: HTTP {r.status_code} {r.text[:200]}")


def poll_interval(elapsed_sec: float) -> float:
    if elapsed_sec < 5:
        return 0.5
    if elapsed_sec < 15:
        return 1.0
    return 2.0


def wait_ready(url: str, timeout_sec: int, label: str = "backend") -> tuple[bool, dict[str, float]]:
    deadline = time.time() + timeout_sec
    t0 = time.perf_counter()
    last_log = -5.0
    attempts = 0
    last_status: str | int = "connecting"
    LOG.info("%s waiting for model ready at %s (timeout=%ds)", label, url, timeout_sec)
    client = httpx.Client(timeout=5.0)
    try:
        while time.time() < deadline:
            elapsed = time.perf_counter() - t0
            attempts += 1
            try:
                r = client.get(f"{url}/v1/models")
                last_status = r.status_code
                if r.status_code == 200:
                    LOG.info(
                        "%s ready after %.1fs (%d polls, last=%s)",
                        label,
                        elapsed,
                        attempts,
                        last_status,
                    )
                    return True, {
                        "ready_wait_sec": elapsed,
                        "ready_polls": attempts,
                    }
            except Exception as exc:
                last_status = f"connect:{type(exc).__name__}"

            if elapsed - last_log >= 5.0:
                LOG.info(
                    "%s still loading after %.1fs (last=%s, polls=%d)",
                    label,
                    elapsed,
                    last_status,
                    attempts,
                )
                last_log = elapsed

            time.sleep(poll_interval(elapsed))
    finally:
        client.close()

    elapsed = time.perf_counter() - t0
    LOG.warning(
        "%s not ready after %.1fs (%d polls, last=%s)",
        label,
        elapsed,
        attempts,
        last_status,
    )
    return False, {"ready_wait_sec": elapsed, "ready_polls": attempts}


def ensure_exclusive(target: str) -> dict[str, float]:
    timings: dict[str, float] = {}
    if not ROUTER_EXCLUSIVE:
        return timings
    target_container = backend_container(target)
    t0 = time.perf_counter()
    for name, container in BACKEND_CONTAINERS.items():
        if name != target:
            docker_stop(container)
    timings["stop_sec"] = time.perf_counter() - t0
    t1 = time.perf_counter()
    docker_start(target_container)
    timings["start_sec"] = time.perf_counter() - t1
    ready, ready_meta = wait_ready(
        backend_url(target),
        READY_TIMEOUT_SEC,
        label=f"{target} backend",
    )
    timings.update(ready_meta)
    if not ready:
        raise RuntimeError(f"backend {target} not ready within {READY_TIMEOUT_SEC}s")
    return timings


def backend_ready_quick(name: str) -> bool:
    try:
        with httpx.Client(timeout=2.0) as client:
            r = client.get(f"{backend_url(name)}/v1/models")
            return r.status_code == 200
    except Exception:
        return False


def switch_to(target: str) -> None:
    # If a VL pre-pass is actively running its inference, don't steal the VL backend.
    # Wait until the pass finishes (up to 130s = VL_PASS_MAX_TOKENS / ~8 tok/s + margin).
    if target != "vl" and STATE.vl_pass_active.is_set():
        LOG.info("switch to %s deferred: waiting for vl_pass to complete", target)
        completed = STATE.vl_pass_active.wait(timeout=130.0)
        # wait() returns False on timeout — proceed anyway rather than hanging forever
        if not completed:
            LOG.warning("vl_pass still active after 130s wait; proceeding with switch to %s", target)

    with STATE.lock:
        container = backend_container(target)
        if STATE.active == target and docker_running(container) and backend_ready_quick(target):
            return
        if STATE.active == target:
            ensure_exclusive(target)
            return
        t0 = time.perf_counter()
        LOG.info("switch %s -> %s", STATE.active, target)
        timings = ensure_exclusive(target)
        STATE.active = target
        STATE.last_switch_sec = time.perf_counter() - t0
        STATE.switch_count += 1
        STATE.touch()
        LOG.info(
            "switch done in %.1fs (count=%d, stop=%.1fs, start=%.1fs, ready=%.1fs, polls=%d)",
            STATE.last_switch_sec,
            STATE.switch_count,
            timings.get("stop_sec", 0.0),
            timings.get("start_sec", 0.0),
            timings.get("ready_wait_sec", 0.0),
            int(timings.get("ready_polls", 0)),
        )


def switch_to_vl() -> None:
    switch_to("vl")


def extract_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    if "messages" in body and isinstance(body["messages"], list):
        return body["messages"]
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    if isinstance(prompt, list) and prompt and isinstance(prompt[0], str):
        return [{"role": "user", "content": "\n".join(prompt)}]
    return []


def message_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
    return "\n".join(parts)


def count_tokens_on(url: str, text: str) -> int | None:
    if not text:
        return 0
    try:
        r = httpx.post(
            f"{url}/tokenize",
            json={"content": text, "add_special": False},
            timeout=120.0,
        )
        if r.status_code != 200:
            return None
        return len(r.json().get("tokens", []))
    except Exception as exc:
        LOG.warning("tokenize failed on %s: %s", url, exc)
        return None


def estimate_tokens(body: dict[str, Any]) -> int:
    messages = extract_messages(body)
    text = message_text(messages)
    if not text:
        return 0

    with STATE.lock:
        active_url = backend_url(STATE.active)

    # tokenize on currently running backend; fallback to char heuristic
    if docker_running(FAST_CONTAINER if STATE.active == "fast" else LONG_CONTAINER):
        n = count_tokens_on(active_url, text)
        if n is not None:
            return n + max(0, len(messages) * 4)

    return max(1, len(text) // 3) + len(messages) * 4


def choose_backend(body: dict[str, Any] | None) -> str:
    if body is None:
        with STATE.lock:
            return STATE.active

    tokens = estimate_tokens(body)
    with STATE.lock:
        if STATE.active == "long" and STATE.long_sticky():
            LOG.info("route long (sticky) tokens=%d", tokens)
            return "long"
        if tokens > TOKEN_THRESHOLD:
            LOG.info("route long tokens=%d > %d", tokens, TOKEN_THRESHOLD)
            return "long"
        LOG.info("route fast tokens=%d", tokens)
        return "fast"


def router_status() -> dict[str, Any]:
    with STATE.lock:
        idle_left = max(0.0, LONG_IDLE_TTL_SEC - (time.time() - STATE.last_activity))
        return {
            "active_backend": STATE.active,
            "exclusive": ROUTER_EXCLUSIVE,
            "token_threshold": TOKEN_THRESHOLD,
            "long_idle_ttl_sec": LONG_IDLE_TTL_SEC,
            "long_sticky_active": STATE.long_sticky(),
            "idle_until_fast_sec": round(idle_left, 1) if STATE.active == "long" else 0,
            "last_switch_sec": STATE.last_switch_sec,
            "switch_count": STATE.switch_count,
            "fast_running": docker_running(FAST_CONTAINER),
            "long_running": docker_running(LONG_CONTAINER),
            "vl_running": docker_running(VL_CONTAINER),
            "fast_url": FAST_URL,
            "long_url": LONG_URL,
            "vl_url": VL_URL,
            "vl_pass_enabled": VL_PASS_ENABLED,
            "router_main_backend": os.getenv("ROUTER_MAIN_BACKEND", "fast"),
        }


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length > 0 else b""

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _is_allowed_path(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES)

    def _handle_agent_runs(self) -> None:
        """Cursor SDK-style run metadata + SSE event stream."""
        path = self.path.split("?", 1)[0]
        parts = [p for p in path.split("/") if p]

        # /router/agent/runs
        # /router/agent/runs/{id}
        # /router/agent/runs/{id}/events
        if len(parts) < 3 or parts[0] != "router" or parts[1] != "agent" or parts[2] != "runs":
            self.send_response(404)
            self.end_headers()
            return

        if self.command == "GET" and len(parts) == 3:
            self._send_json(200, {"runs": list_runs()})
            return

        if len(parts) >= 4:
            run_id = parts[3]
            if self.command == "GET" and len(parts) == 4:
                run = get_run(run_id)
                if not run:
                    self._send_json(404, {"error": "run not found"})
                    return
                self._send_json(
                    200,
                    {**run.to_meta(), "events": run.events, "supports": run.to_meta()["supports"]},
                )
                return

            if self.command == "GET" and len(parts) == 5 and parts[4] == "events":
                run = get_run(run_id)
                if not run:
                    self._send_json(404, {"error": "run not found"})
                    return
                after_seq = 0
                qs = self.path.split("?", 1)
                if len(qs) > 1:
                    for param in qs[1].split("&"):
                        if param.startswith("after_seq="):
                            try:
                                after_seq = int(param.split("=", 1)[1])
                            except ValueError:
                                pass
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    for chunk in stream_events_sse(run_id, after_seq=after_seq):
                        self.wfile.write(chunk.encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    LOG.info("agent run SSE client disconnected run_id=%s", run_id)
                return

        self.send_response(404)
        self.end_headers()

    def _proxy(self) -> None:
        if self.path.startswith("/router/status"):
            self._send_json(200, router_status())
            return

        if self.path.startswith("/router/agent/runs"):
            self._handle_agent_runs()
            return

        if not self._is_allowed_path(self.path):
            self.send_response(404)
            self.end_headers()
            return

        body_bytes = self._read_body() if self.command in ("POST", "PUT", "PATCH") else b""
        body_json: dict[str, Any] | None = None
        if body_bytes:
            ctype = self.headers.get("Content-Type", "")
            if "application/json" in ctype:
                try:
                    parsed = json.loads(body_bytes.decode("utf-8"))
                    if isinstance(parsed, dict):
                        body_json = parsed
                except json.JSONDecodeError:
                    pass

        req_headers = {k: v for k, v in self.headers.items()}
        capture_id = maybe_capture(self.path, body_bytes, body_json, req_headers)
        flow_id: str | None = None
        run_id: str | None = None
        t_request = time.perf_counter()
        if body_json and self.path.startswith("/v1/chat/completions"):
            cursor_original = copy.deepcopy(body_json)
            flow_id = begin_flow(cursor_original, req_headers, capture_id)
            run_id = flow_id  # aligned with req_id inside process_two_pass

        proxy_body = body_json
        proxy_bytes = body_bytes
        target: str | None = None
        intent_result = None
        agent_phase = ""
        two_pass_stats = None
        force_non_stream = False
        vl_pass_ran = False

        if body_json and self.path.startswith("/v1/chat/completions"):
            # VL pre-pass: only when the LATEST user message itself contains an image.
            # Images in older turns are already processed — don't re-trigger VL on follow-ups.
            if VL_PASS_ENABLED and last_user_message_has_image(body_json.get("messages", [])):
                query_for_vl = extract_last_user_query(body_json)
                img_n = count_images(body_json.get("messages", []))
                try:
                    # Mark VL pass as active AFTER VL backend becomes ready.
                    # Any concurrent switch_to(non-vl) will wait for this event to clear.
                    def _switch_to_vl_and_mark() -> None:
                        switch_to_vl()
                        STATE.vl_pass_active.set()

                    body_json, vl_pass_ran = maybe_vl_preprocess(
                        body_json,
                        query_for_vl,
                        VL_URL,
                        _switch_to_vl_and_mark,
                    )
                    LOG.info("vl_preprocess images=%d ran=%s", img_n, str(vl_pass_ran).lower())
                except Exception as exc:
                    LOG.warning("vl_preprocess failed images=%d: %s", img_n, exc)
                finally:
                    STATE.vl_pass_active.clear()

            # Multimodal (mmproj): keep images. Legacy text-only: strip images + flatten arrays.
            msgs = body_json.get("messages", [])
            if MULTIMODAL_BACKEND and has_real_image_content(msgs if isinstance(msgs, list) else []):
                body_json, norm_stats = normalize_messages_for_multimodal(body_json)
                if norm_stats.get("flattened", 0) or norm_stats.get("kept_image_parts", 0):
                    LOG.info(
                        "normalize_multimodal flattened=%d kept_images=%d",
                        norm_stats.get("flattened", 0),
                        norm_stats.get("kept_image_parts", 0),
                    )
            else:
                body_json, norm_stats = normalize_messages_for_coder(body_json)
                if norm_stats.get("flattened", 0) or norm_stats.get("stripped_image_parts", 0):
                    LOG.info(
                        "normalize_coder flattened=%d stripped_images=%d",
                        norm_stats.get("flattened", 0),
                        norm_stats.get("stripped_image_parts", 0),
                    )

            if TWO_PASS_ROUTER:
                with STATE.lock:
                    sticky = STATE.long_sticky()
                    current_active = STATE.active
                proxy_body, target, two_pass_stats, intent_result, agent_phase = process_two_pass(
                    body_json, sticky_long=sticky, active_backend=current_active
                )
                run_id = two_pass_stats.req_id if two_pass_stats else run_id
                set_current_run_id(run_id)
                set_trace_context(
                    flow_id=flow_id or "",
                    run_id=run_id or "",
                    backend=target or "",
                    intent=intent_result.intent if intent_result else "",
                    phase=agent_phase or "tool_planning",
                )
                proxy_bytes = json.dumps(proxy_body, ensure_ascii=False).encode("utf-8")
                if flow_id and proxy_body:
                    record_proxy(
                        flow_id,
                        proxy_body,
                        intent=intent_result.intent if intent_result else "",
                        phase=agent_phase or "tool_planning",
                        backend=target or "",
                        route_reason=two_pass_stats.route_reason if two_pass_stats else "",
                        raw_tokens=two_pass_stats.raw_tokens if two_pass_stats else 0,
                        pack_tokens=two_pass_stats.pack_tokens if two_pass_stats else 0,
                        saved_pct=two_pass_stats.saved_pct if two_pass_stats else 0.0,
                    )
                if run_id and proxy_body:
                    try:
                        from adapters.observe import emit_status, emit_task

                        emit_status(
                            run_id,
                            "routing",
                            f"backend={target or '?'} intent={intent_result.intent if intent_result else '?'}",
                        )
                        emit_task(
                            run_id,
                            "proxy.built",
                            f"phase={agent_phase or 'tool_planning'} saved={two_pass_stats.saved_pct if two_pass_stats else 0}%",
                        )
                    except ImportError:
                        pass

                # Don't cold-switch for casual/explain — serve on current active backend.
                # Switching costs 12-20s for a question that any model can answer.
                if (
                    target != current_active
                    and intent_result
                    and intent_result.intent in ("casual", "explain")
                ):
                    LOG.info(
                        "no_switch intent=%s target=%s → using active=%s",
                        intent_result.intent, target, current_active,
                    )
                    target = current_active

                if intent_result and (
                    agent_phase in ("final_answer", "partial_final_answer", "recovery_final", "tool_planning")
                    or (
                        intent_result.needs_tools
                        and (
                            is_exec_intent(intent_result.intent)
                            or intent_result.intent
                            in ("agent", "debug", "shell_task", "read_only_analysis", "project_inspection")
                        )
                    )
                ):
                    force_non_stream = True
            else:
                from chat_fast import build_simple_chat_body, est_message_chars, is_simple_qa, strip_agent_fields

                chat_fast = False
                messages = body_json.get("messages", [])
                if isinstance(messages, list) and is_simple_qa(messages):
                    proxy_body = build_simple_chat_body(body_json)
                    strip_agent_fields(proxy_body)
                    proxy_bytes = json.dumps(proxy_body, ensure_ascii=False).encode("utf-8")
                    chat_fast = True
                    LOG.info(
                        "chat_fast route=fast tools_stripped=true messages=%d est_chars=%d max_tokens=%s",
                        len(proxy_body.get("messages", [])),
                        est_message_chars(proxy_body),
                        proxy_body.get("max_tokens"),
                    )
                else:
                    proxy_body = body_json
                    proxy_bytes = json.dumps(proxy_body, ensure_ascii=False).encode("utf-8")
                if chat_fast:
                    target = "fast"

        if target is None:
            target = choose_backend(proxy_body)
        try:
            switch_to(target)
        except Exception as exc:
            LOG.exception("switch failed")
            self._send_json(503, {"error": {"message": str(exc), "type": "router_switch_error"}})
            return

        STATE.touch()
        url = backend_url(target) + self.path
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP
            and k.lower() not in ("host", "content-length")
        }

        try:
            stream = False
            if not force_non_stream and proxy_body and proxy_body.get("stream") is True:
                stream = True

            read_sec = float(os.getenv("LLM_READ_TIMEOUT_SEC", "120"))
            timeout = httpx.Timeout(connect=30.0, read=read_sec, write=60.0, pool=30.0)
            gw_path = self.path if self.path.startswith("/") else "/v1/chat/completions"

            if stream:
                gw_stream = chat_completion(
                    method=self.command,
                    path=gw_path,
                    headers=headers,
                    body_bytes=proxy_bytes if proxy_bytes else None,
                    body_json=proxy_body,
                    backend_hint=target or "long",
                    stream=True,
                    timeout=timeout,
                )
                if not isinstance(gw_stream, GatewayStream):
                    raise TypeError("expected GatewayStream")
                self.send_response(gw_stream.status_code)
                self.send_header("Connection", "close")
                for k, v in gw_stream.headers.items():
                    if k.lower() not in HOP_BY_HOP:
                        self.send_header(k, v)
                self.end_headers()
                tail = b""
                for chunk in gw_stream.iter_bytes():
                    if not chunk:
                        continue
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    tail = (tail + chunk)[-64:]
                    if SSE_DONE_MARKER in tail:
                        break
                gw_stream.close()
            else:
                t_llm = time.perf_counter()
                gw = chat_completion(
                    method=self.command,
                    path=gw_path,
                    headers=headers,
                    body_bytes=proxy_bytes if proxy_bytes else None,
                    body_json=proxy_body,
                    backend_hint=target or "long",
                    stream=False,
                    timeout=timeout,
                )
                if not isinstance(gw, GatewayResult):
                    raise TypeError("expected GatewayResult")
                llm_wait_ms = (time.perf_counter() - t_llm) * 1000.0
                LOG.info(
                    "llm_done req=%s backend=%s status=%d latency_ms=%.0f pack_tokens=%s phase=%s",
                    flow_id or "",
                    target or "",
                    gw.status_code,
                    llm_wait_ms,
                    (two_pass_stats.pack_tokens if two_pass_stats else "?"),
                    agent_phase or "tool_planning",
                )
                if agent_phase in ("final_answer", "partial_final_answer", "recovery_final") and gw.status_code == 200:
                    try:
                        _resp_preview = gw.json_data or json.loads(out_bytes.decode("utf-8"))
                        _msg = (_resp_preview.get("choices") or [{}])[0].get("message") or {}
                        _reasoning = str(
                            _msg.get("reasoning_content")
                            or _msg.get("thinking")
                            or _msg.get("reasoning")
                            or ""
                        ).strip()
                        _content = str(_msg.get("content") or "").strip()
                        if _reasoning:
                            LOG.info("final_thinking req=%s %s", flow_id or "", _reasoning[:2000])
                        elif _content:
                            LOG.info(
                                "final_answer_preview req=%s chars=%d %s",
                                flow_id or "",
                                len(_content),
                                _content[:600].replace("\n", " ↵ "),
                            )
                        ap = getattr(two_pass_stats.mem_state, "agent_plan", None) or {}
                        _expl = str(ap.get("exploration_thinking") or "").strip()
                        if _expl:
                            try:
                                from explorer_trace import trace_explorer

                                trace_explorer("final_synthesis", thinking=_expl[:4000])
                            except ImportError:
                                pass
                    except (KeyError, IndexError, TypeError, json.JSONDecodeError, AttributeError):
                        pass
                resp_status = gw.status_code
                out_bytes = gw.content
                if resp_status == 400 and proxy_body:
                    try:
                        err_json = gw.json_data or json.loads(out_bytes.decode("utf-8"))
                        err = err_json.get("error") or {}
                        if err.get("type") == "exceed_context_size_error" or "exceed" in str(
                            err.get("message", "")
                        ).lower():
                            from runtime_core.prompt_enforcer import (
                                emergency_shrink,
                                record_ctx_overflow,
                                record_ctx_success,
                            )

                            n_ctx = int(err.get("n_ctx") or 32768)
                            n_prompt = int(err.get("n_prompt_tokens") or 0)
                            record_ctx_overflow(n_prompt, n_ctx)
                            shrunk = emergency_shrink(proxy_body, n_ctx)
                            proxy_body = shrunk
                            proxy_bytes = json.dumps(shrunk, ensure_ascii=False).encode("utf-8")
                            LOG.warning(
                                "ctx_overflow retry req shrunk msgs=%d",
                                len(shrunk.get("messages", [])),
                            )
                            gw2 = chat_completion(
                                method=self.command,
                                path=gw_path,
                                headers=headers,
                                body_bytes=proxy_bytes,
                                body_json=proxy_body,
                                backend_hint=target or "long",
                                stream=False,
                                timeout=timeout,
                            )
                            if isinstance(gw2, GatewayResult):
                                resp_status = gw2.status_code
                                out_bytes = gw2.content
                                gw = gw2
                            if resp_status == 200:
                                try:
                                    u = (gw.json_data or {}).get("usage") or {}
                                    record_ctx_success(int(u.get("prompt_tokens") or 0))
                                except (TypeError, ValueError):
                                    pass
                    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                        pass
                raw_resp_json: dict[str, Any] | None = None
                processed_resp_json: dict[str, Any] | None = None
                if (
                    intent_result
                    and intent_result.needs_tools
                    and (
                        is_exec_intent(intent_result.intent)
                        or agent_phase
                        or intent_result.intent in ("read_only_analysis", "project_inspection")
                    )
                    and resp_status == 200
                    and proxy_body
                ):
                    try:
                        resp_json = json.loads(out_bytes.decode("utf-8"))
                        raw_resp_json = copy.deepcopy(resp_json)
                        query = extract_last_user_query(body_json) if body_json else ""
                        phase = agent_phase or (
                            "final_answer" if not proxy_body.get("tools") else "tool_planning"
                        )

                        def _retry_call(retry_body: dict[str, Any]) -> dict[str, Any]:
                            return chat_completion_retry(
                                method=self.command,
                                path=gw_path,
                                headers=headers,
                                body_json=retry_body,
                                backend_hint=target or "long",
                                timeout=timeout,
                            )

                        # Log raw model output preview before postprocessing
                        try:
                            _raw_msg = resp_json["choices"][0]["message"]
                            _raw_tc = _raw_msg.get("tool_calls") or []
                            _raw_content = str(_raw_msg.get("content") or "")[:120].replace("\n", "↵")
                            LOG.info(
                                "llm_raw phase=%s tool_calls=%d content_preview=%r",
                                phase or "tool_planning",
                                len(_raw_tc),
                                _raw_content,
                            )
                        except (KeyError, IndexError, TypeError):
                            pass

                        if phase == "final_answer" or agent_phase in (
                            "partial_final_answer",
                            "recovery_final",
                        ):
                            resp_json, _exec_log = postprocess_agent_response(
                                resp_json,
                                intent_result.intent,
                                query,
                                phase="final_answer" if phase == "final_answer" else agent_phase,
                                session_state=two_pass_stats.mem_state,
                            )
                        else:
                            resp_json, _exec_log = postprocess_agent_response(
                                resp_json,
                                intent_result.intent,
                                query,
                                retry_call=_retry_call,
                                retry_body=proxy_body,
                                phase="tool_planning",
                                available_tools=body_json.get("tools"),
                                proxy_messages=proxy_body.get("messages"),
                                session_state=two_pass_stats.mem_state,
                            )
                        processed_resp_json = resp_json
                        try:
                            phase_for_reasoning = agent_phase or (
                                "final_answer"
                                if not proxy_body.get("tools")
                                else "tool_planning"
                            )
                            usage = resp_json.get("usage") if isinstance(resp_json.get("usage"), dict) else {}
                            llm_elapsed = time.perf_counter() - t_llm
                            total_elapsed = time.perf_counter() - t_request

                            from runtime_inspector import (
                                build_inspector_from_state,
                                inject_runtime_inspector,
                                runtime_inspector_enabled,
                            )

                            if runtime_inspector_enabled() and two_pass_stats and two_pass_stats.mem_state:
                                inspector_md = build_inspector_from_state(
                                    two_pass_stats.mem_state,
                                    run_id=run_id or "",
                                    query=query,
                                    phase=phase_for_reasoning,
                                    stats=two_pass_stats,
                                    intent=intent_result.intent if intent_result else "",
                                    backend=target or "",
                                    llm_elapsed_sec=llm_elapsed,
                                    total_elapsed_sec=total_elapsed,
                                    cursor_message_count=len(body_json.get("messages", []))
                                    if body_json
                                    else 0,
                                    proxy_message_count=len(proxy_body.get("messages", []))
                                    if proxy_body
                                    else 0,
                                    usage=usage,
                                )
                                if inject_runtime_inspector(resp_json, inspector_md):
                                    LOG.info(
                                        "runtime_inspector injected phase=%s chars=%d",
                                        phase_for_reasoning,
                                        len(inspector_md),
                                    )
                            if (
                                phase_for_reasoning in ("final_answer", "partial_final_answer")
                                and two_pass_stats
                                and two_pass_stats.mem_state
                            ):
                                try:
                                    pmsg = resp_json.get("choices", [{}])[0].get("message", {})
                                    if not pmsg.get("tool_calls"):
                                        from reference.loop_guard import record_final_answer_sent

                                        record_final_answer_sent(two_pass_stats.mem_state)
                                except Exception:
                                    pass
                            elif phase_for_reasoning != "tool_planning":
                                from cursor_reasoning import (
                                    cursor_reasoning_enabled,
                                    inject_cursor_reasoning,
                                    reasoning_from_state,
                                )

                                if cursor_reasoning_enabled() and two_pass_stats and two_pass_stats.mem_state:
                                    reasoning = reasoning_from_state(
                                        two_pass_stats.mem_state,
                                        query=query,
                                        phase=phase_for_reasoning,
                                    )
                                    if inject_cursor_reasoning(resp_json, reasoning):
                                        LOG.info(
                                            "cursor_reasoning injected phase=%s chars=%d",
                                            phase_for_reasoning,
                                            len(reasoning),
                                        )
                        except Exception as exc:
                            LOG.warning("runtime_inspector inject failed: %s", exc)
                        out_bytes = json.dumps(resp_json, ensure_ascii=False).encode("utf-8")
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        LOG.warning("agent_exec postprocess skipped: invalid JSON response")
                elif resp_status == 200 and proxy_body:
                    try:
                        resp_json = json.loads(out_bytes.decode("utf-8"))
                        if intent_result and intent_result.intent in ("explain", "casual"):
                            phase = ""
                        elif agent_phase in (
                            "tool_planning",
                            "final_answer",
                            "partial_final_answer",
                            "recovery_final",
                        ):
                            phase = agent_phase
                        elif not proxy_body.get("tools"):
                            phase = "final_answer"
                        else:
                            phase = ""
                        resp_json = finalize_client_response(
                            resp_json,
                            intent_result.intent if intent_result else "",
                            phase,
                        )
                        processed_resp_json = resp_json
                        out_bytes = json.dumps(resp_json, ensure_ascii=False).encode("utf-8")
                    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, IndexError):
                        LOG.warning("client response finalize skipped: invalid JSON")

                if resp_status == 200 and out_bytes:
                    try:
                        from reference.response_guard import apply_nonempty_guard

                        guard_json = json.loads(out_bytes.decode("utf-8"))
                        guard_json, _ = apply_nonempty_guard(
                            guard_json,
                            phase=agent_phase or "",
                            intent_name=intent_result.intent if intent_result else "",
                            query=extract_last_user_query(body_json) if body_json else "",
                            session_state=two_pass_stats.mem_state if two_pass_stats else None,
                            reason="main_final_gate",
                        )
                        out_bytes = json.dumps(guard_json, ensure_ascii=False).encode("utf-8")
                        if processed_resp_json is not None:
                            processed_resp_json = guard_json
                    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ImportError) as exc:
                        LOG.warning("main response_guard skipped: %s", exc)

                if run_id and resp_status == 200:
                    try:
                        if raw_resp_json is None:
                            raw_resp_json = json.loads(out_bytes.decode("utf-8"))
                        if processed_resp_json is None:
                            processed_resp_json = raw_resp_json
                        if flow_id:
                            record_response(
                                flow_id,
                                raw_resp_json,
                                elapsed_sec=time.perf_counter() - t_llm,
                                phase=agent_phase or (
                                    "final_answer"
                                    if proxy_body and not proxy_body.get("tools")
                                    else "tool_planning"
                                ),
                                processed=processed_resp_json,
                            )
                        try:
                            pmsg = (processed_resp_json or raw_resp_json or {}).get("choices", [{}])[0].get(
                                "message", {}
                            )
                            preview = str(pmsg.get("content") or "")[:300]
                            choice = (processed_resp_json or raw_resp_json or {}).get("choices", [{}])[0]
                            finish_reason = str(choice.get("finish_reason") or "")
                            run_status = "partial" if finish_reason == "length" else "finished"
                            finish_run(
                                run_id,
                                status=run_status,
                                phase=agent_phase or "",
                                intent=intent_result.intent if intent_result else "",
                                backend=target or "",
                                result_preview=preview,
                                error="output_truncated" if run_status == "partial" else "",
                            )
                        except (KeyError, IndexError, TypeError):
                            finish_run(run_id, status="finished")
                    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                        LOG.warning("flow trace step 3 skipped: invalid JSON response")
                        finish_run(run_id, status="error", error="invalid JSON response")
                elif run_id and resp_status != 200:
                    finish_run(run_id, status="error", error=f"http {resp_status}")
                if run_id:
                    from adapters.trace import emit_runtime_event
                    from runtime_core.runtime_events import event_turn_end

                    blocked = ""
                    if two_pass_stats and getattr(two_pass_stats, "mem_state", None):
                        lt = getattr(two_pass_stats.mem_state, "last_runtime_turn", None) or {}
                        if isinstance(lt, dict):
                            blocked = str(lt.get("final_blocked_reason") or "")
                    emit_runtime_event(
                        event_turn_end(
                            final_allowed=resp_status == 200 and not blocked,
                            final_blocked_reason=blocked,
                            total_latency_ms=(time.perf_counter() - t_request) * 1000.0,
                            backend=target or "",
                            intent=intent_result.intent if intent_result else "",
                            phase=agent_phase or "tool_planning",
                            flow_id=flow_id or "",
                            run_id=run_id or "",
                        )
                    )
                    set_current_run_id(None)

                client_wants_stream = bool(body_json and body_json.get("stream") is True)
                if client_wants_stream and force_non_stream:
                    try:
                        resp_json = json.loads(out_bytes.decode("utf-8"))
                        phase_for_sse = agent_phase or (
                            "final_answer" if not proxy_body.get("tools") else "tool_planning"
                        )
                        out_bytes = completion_json_to_sse(resp_json, phase=phase_for_sse)
                    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, IndexError):
                        LOG.warning("sse wrap skipped: invalid completion JSON")

                self.send_response(resp_status)
                if client_wants_stream and force_non_stream:
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                else:
                    for k, v in gw.headers.items():
                        lk = k.lower()
                        if lk not in HOP_BY_HOP and lk != "content-length":
                            self.send_header(k, v)
                    self.send_header("Content-Length", str(len(out_bytes)))
                self.end_headers()
                self.wfile.write(out_bytes)
        except BrokenPipeError:
            LOG.info("client disconnected during stream proxy")
        except ConnectionResetError:
            LOG.info("client reset connection during stream proxy")
        except Exception as exc:
            LOG.exception("proxy error")
            self._send_json(502, {"error": {"message": str(exc), "type": "router_proxy_error"}})

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()


def bootstrap() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    LOG.info(
        "router starting exclusive=%s threshold=%d ttl=%ds default=%s",
        ROUTER_EXCLUSIVE,
        TOKEN_THRESHOLD,
        LONG_IDLE_TTL_SEC,
        DEFAULT_BACKEND,
    )
    try:
        switch_to(DEFAULT_BACKEND if DEFAULT_BACKEND in ("fast", "long") else "fast")
    except Exception:
        LOG.exception("bootstrap switch failed; will retry on first request")


def main() -> None:
    bootstrap()
    server = ThreadingHTTPServer((ROUTER_HOST, ROUTER_PORT), ProxyHandler)
    LOG.info("listening on %s:%d", ROUTER_HOST, ROUTER_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
