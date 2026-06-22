#!/usr/bin/env python3
"""Gateway backend swap benchmark — llama_cpp vs litellm vs mock.

Usage:
  BACKEND=mock python3 scripts/benchmark-gateway-swap.py
  BACKEND=litellm   python3 scripts/benchmark-gateway-swap.py
  BACKEND=llama_cpp python3 scripts/benchmark-gateway-swap.py

Live engines (required when GATEWAY_LIVE=1):
  ./scripts/start-gateway-live.sh
  GATEWAY_LIVE=1 LONG_URL=http://127.0.0.1:8082 BACKEND=llama_cpp python3 ...
  GATEWAY_LIVE=1 LITELLM_URL=http://127.0.0.1:4000 BACKEND=litellm python3 ...
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

os.environ.setdefault("OTEL_EVENT_CAPTURE", "1")
os.environ.setdefault("OTEL_FLOW_TRACE", "1")
os.environ.setdefault("GATEWAY_BACKEND", os.getenv("BACKEND", "mock"))

from adapters.gateway import (  # noqa: E402
    GatewayResult,
    GatewayStream,
    active_backend,
    chat_completion,
    normalize_completion_response,
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class BackendReport:
    backend: str
    pass_rate: str
    stream: str
    usage: str
    tool_call: str
    latency_ms: float
    checks: list[CheckResult]
    skipped: bool = False
    skip_reason: str = ""


def _body(*, stream: bool = False, tools: bool = False) -> dict:
    user_content = (
        "Read the file README.md using the Read tool."
        if tools
        else "Say hello in one word."
    )
    body: dict = {
        "model": "test-model",
        "messages": [{"role": "user", "content": user_content}],
        "max_tokens": 128 if tools else 32,
        "stream": stream,
    }
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "description": "read file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ]
        body["tool_choice"] = "auto"
    return body


def _validate_llm_completed_event(*, require_tokens: bool) -> tuple[bool, str]:
    from adapters.trace import get_recorded_events

    llm_events = [e for e in get_recorded_events() if e.get("event") == "llm.completed"]
    if not llm_events:
        return False, "llm.completed event missing"
    llm = llm_events[-1]
    missing = []
    for key in ("gateway_backend", "latency_ms"):
        if llm.get(key) in (None, ""):
            missing.append(key)
    if require_tokens:
        for key in ("prompt_tokens", "completion_tokens"):
            if int(llm.get(key) or -1) < 0:
                missing.append(key)
    if missing:
        return False, f"llm.completed missing {','.join(missing)}"
    return True, (
        f"backend={llm.get('gateway_backend')} "
        f"latency={llm.get('latency_ms')}ms "
        f"prompt={llm.get('prompt_tokens')} completion={llm.get('completion_tokens')}"
    )


def _check_non_stream(gw_mod=None, *, tools: bool = False) -> CheckResult:
    import adapters.gateway as _gw
    from adapters.trace import clear_recorded_events

    gwapi = gw_mod or _gw
    clear_recorded_events()
    body = _body(stream=False, tools=tools)
    gw = gwapi.chat_completion(
        method="POST",
        path="/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        body_bytes=json.dumps(body).encode(),
        body_json=body,
        backend_hint="long",
        stream=False,
    )
    if not isinstance(gw, gwapi.GatewayResult):
        return CheckResult("non_stream", False, "not GatewayResult")
    if gw.status_code != 200:
        return CheckResult("non_stream", False, f"status={gw.status_code} err={gw.error}")

    ok_evt, evt_detail = _validate_llm_completed_event(require_tokens=gwapi.active_backend() != "mock")
    if not ok_evt:
        check_name = "tool_call" if tools else "non_stream"
        return CheckResult(check_name, False, evt_detail)

    data = gw.json_data or {}
    norm = gwapi.normalize_completion_response(data)
    usage = norm.get("usage") or {}
    if not usage.get("total_tokens") and gwapi.active_backend() != "mock":
        return CheckResult("non_stream", False, "missing usage")
    msg = norm["choices"][0]["message"]
    if tools:
        if not msg.get("tool_calls"):
            return CheckResult("tool_call", False, "no tool_calls in response")
        return CheckResult("tool_call", True, f"{evt_detail} latency={gw.metrics.latency_ms}ms")
    if not str(msg.get("content") or "").strip() and gwapi.active_backend() != "mock":
        return CheckResult("non_stream", False, "empty content")
    return CheckResult(
        "non_stream",
        True,
        f"tokens={usage.get('total_tokens')} {evt_detail}",
    )


def _check_stream(gw_mod=None) -> CheckResult:
    import adapters.gateway as _gw

    gwapi = gw_mod or _gw
    body = _body(stream=True)
    gw = gwapi.chat_completion(
        method="POST",
        path="/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        body_bytes=json.dumps(body).encode(),
        body_json=body,
        backend_hint="long",
        stream=True,
    )
    if not isinstance(gw, gwapi.GatewayStream):
        if isinstance(gw, gwapi.GatewayResult):
            return CheckResult("stream", False, f"got GatewayResult status={gw.status_code}")
        return CheckResult("stream", False, "not GatewayStream")
    chunks = b"".join(gw.iter_bytes())
    gw.close()
    if gw.status_code != 200:
        return CheckResult("stream", False, f"status={gw.status_code}")
    if b"data:" not in chunks:
        return CheckResult("stream", False, "no SSE data lines")
    return CheckResult("stream", True, f"bytes={len(chunks)}")


def _check_error_normalize() -> CheckResult:
    from adapters.gateway import normalize_error_response

    err = normalize_error_response(429, "rate limit", error_type="rate_limit_error")
    if err.get("error", {}).get("type") != "rate_limit_error":
        return CheckResult("error_handling", False, "bad error shape")
    return CheckResult("error_handling", True, "normalized")


def _check_connection_error(gw_mod=None) -> CheckResult:
    """Live backends must map connection failure → 502 + connection_error + llm.completed."""
    import importlib
    import adapters.gateway as _gw

    gwapi = gw_mod or _gw
    if gwapi.active_backend() == "mock":
        return CheckResult("connection_error", True, "mock skip")

    dead_url = os.getenv("GATEWAY_DEAD_URL", "http://127.0.0.1:59999")
    saved: dict[str, str | None] = {
        "LONG_URL": os.environ.get("LONG_URL"),
        "LITELLM_URL": os.environ.get("LITELLM_URL"),
    }
    try:
        if gwapi.active_backend() == "litellm":
            os.environ["LITELLM_URL"] = dead_url
        else:
            os.environ["LONG_URL"] = dead_url
        importlib.reload(gwapi)
        from adapters.trace import clear_recorded_events

        clear_recorded_events()
        body = _body()
        gw = gwapi.chat_completion(
            method="POST",
            path="/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            body_bytes=json.dumps(body).encode(),
            body_json=body,
            backend_hint="long",
            stream=False,
        )
        if not isinstance(gw, gwapi.GatewayResult):
            return CheckResult("connection_error", False, "not GatewayResult")
        if gw.status_code != 502:
            return CheckResult("connection_error", False, f"expected 502 got {gw.status_code}")
        err_type = gw.metrics.error_type or (gw.json_data or {}).get("error", {}).get("type", "")
        if err_type != "connection_error":
            return CheckResult("connection_error", False, f"error_type={err_type}")
        ok_evt, evt_detail = _validate_llm_completed_event(require_tokens=False)
        if not ok_evt:
            return CheckResult("connection_error", False, evt_detail)
        return CheckResult("connection_error", True, f"502 connection_error {evt_detail}")
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        importlib.reload(gwapi)


def _check_request_shape() -> CheckResult:
    body = _body()
    raw = json.dumps(body).encode()
    if b"messages" not in raw:
        return CheckResult("request_normalize", False, "missing messages")
    return CheckResult("request_normalize", True, "openai-compatible")


def _backend_live_required() -> bool:
    return os.getenv("GATEWAY_LIVE", "0") == "1"


def _litellm_provider_skip() -> tuple[bool, str]:
    """Only litellm may SKIP when provider config is explicitly missing."""
    if os.getenv("LITELLM_PROVIDER_SKIP", "0") == "1":
        return True, "LITELLM_PROVIDER_SKIP=1"
    if not os.getenv("OPENAI_API_KEY") and os.getenv("LITELLM_REQUIRE_PROVIDER_KEY", "0") == "1":
        return True, "provider API key not configured"
    return False, ""


def _probe_url(backend: str) -> str:
    if backend == "litellm":
        base = os.getenv("LITELLM_URL", "http://127.0.0.1:4000").rstrip("/")
        return f"{base}/health"
    return f"{os.getenv('LONG_URL', 'http://127.0.0.1:8082').rstrip('/')}/v1/models"


def _ensure_live_infra(backend: str) -> tuple[bool, str]:
    import urllib.error
    import urllib.request

    url = _probe_url(backend)
    try:
        urllib.request.urlopen(url, timeout=5)
        return True, ""
    except urllib.error.HTTPError as exc:
        if exc.code < 500:
            return True, ""
        return False, f"{url} HTTP {exc.code}"
    except Exception as exc:
        hint = "./scripts/start-gateway-live.sh"
        return False, f"{url} unreachable ({exc}) — run {hint}"


def _probe_backend(gw_mod=None) -> bool:
    import adapters.gateway as _gw

    gw = gw_mod or _gw
    body = _body()
    result = gw.chat_completion(
        method="POST",
        path="/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        body_bytes=json.dumps(body).encode(),
        body_json=body,
        backend_hint="long",
        stream=False,
    )
    if isinstance(result, gw.GatewayResult) and result.metrics.error_type == "connection_error":
        return False
    return isinstance(result, gw.GatewayResult) and result.status_code == 200


def run_backend_report(gw_mod=None) -> BackendReport:
    import adapters.gateway as _gw
    from adapters.trace import clear_recorded_events

    clear_recorded_events()
    gw = gw_mod or _gw
    backend = gw.active_backend()

    if backend == "mock":
        pass
    elif _backend_live_required():
        ok_infra, infra_msg = _ensure_live_infra(backend)
        if not ok_infra:
            if backend == "litellm":
                skip, skip_reason = _litellm_provider_skip()
                if skip:
                    return BackendReport(
                        backend=backend,
                        pass_rate="SKIP",
                        stream="—",
                        usage="—",
                        tool_call="—",
                        latency_ms=0.0,
                        checks=[CheckResult("live_infra", False, infra_msg)],
                        skipped=True,
                        skip_reason=skip_reason,
                    )
            return BackendReport(
                backend=backend,
                pass_rate="FAIL",
                stream="❌",
                usage="❌",
                tool_call="❌",
                latency_ms=0.0,
                checks=[CheckResult("live_infra", False, infra_msg)],
                skipped=False,
            )
        if not _probe_backend(gw):
            return BackendReport(
                backend=backend,
                pass_rate="FAIL",
                stream="❌",
                usage="❌",
                tool_call="❌",
                latency_ms=0.0,
                checks=[CheckResult("live_probe", False, "chat completion probe failed")],
                skipped=False,
            )
    elif backend != "mock":
        if not _probe_backend(gw):
            return BackendReport(
                backend=backend,
                pass_rate="SKIP",
                stream="—",
                usage="—",
                tool_call="—",
                latency_ms=0.0,
                checks=[CheckResult("live", False, "set GATEWAY_LIVE=1 or use BACKEND=mock")],
                skipped=True,
                skip_reason="GATEWAY_LIVE not set",
            )

    checks = [
        _check_request_shape(),
        _check_non_stream(gw, tools=False),
        _check_stream(gw),
        _check_non_stream(gw, tools=True),
        _check_error_normalize(),
    ]
    if _backend_live_required() and backend != "mock":
        checks.append(_check_connection_error(gw))

    ok_n = sum(1 for c in checks if c.ok)
    latencies = []
    for c in checks:
        if "latency=" in c.detail:
            try:
                latencies.append(float(c.detail.split("latency=")[1].split("ms")[0]))
            except ValueError:
                pass
    avg_lat = sum(latencies) / len(latencies) if latencies else 0.0

    def _mark(name: str) -> str:
        for c in checks:
            if c.name == name:
                return "✅" if c.ok else "❌"
        return "—"

    return BackendReport(
        backend=backend,
        pass_rate=f"{ok_n}/{len(checks)}",
        stream=_mark("stream"),
        usage=_mark("non_stream"),
        tool_call=_mark("tool_call"),
        latency_ms=round(avg_lat, 2),
        checks=checks,
    )


def _print_table(reports: list[BackendReport]) -> None:
    print(f"{'backend':<12} {'pass':<8} {'stream':<8} {'usage':<8} {'tool_call':<10} {'latency_ms':>10}")
    print("-" * 62)
    for r in reports:
        print(
            f"{r.backend:<12} {r.pass_rate:<8} {r.stream:<8} {r.usage:<8} {r.tool_call:<10} {r.latency_ms:>10.1f}"
        )
        if r.skip_reason:
            print(f"  skip: {r.skip_reason}")
        for c in r.checks:
            if not c.ok:
                print(f"  FAIL {c.name}: {c.detail}")


def main() -> int:
    mode = os.getenv("GATEWAY_BENCH_MODE", "single").lower()
    reports: list[BackendReport] = []

    if mode == "ab":
        import importlib
        import adapters.gateway as gw_mod

        for backend in ("mock", "llama_cpp", "litellm"):
            os.environ["GATEWAY_BACKEND"] = backend
            os.environ["BACKEND"] = backend
            importlib.reload(gw_mod)
            reports.append(run_backend_report(gw_mod))
    else:
        reports.append(run_backend_report())

    print("=== gateway swap benchmark ===\n")
    _print_table(reports)

    out = ROOT / "tmp" / "benchmark-gateway-swap.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps([asdict(r) for r in reports], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nwritten: {out}")

    live = _backend_live_required()
    all_ok = True
    for r in reports:
        if r.skipped:
            if live and r.backend in ("llama_cpp", "litellm"):
                if r.backend == "litellm" and r.skip_reason:
                    continue
                all_ok = False
            continue
        parts = r.pass_rate.split("/")
        if len(parts) != 2 or parts[0] != parts[1]:
            all_ok = False

    if mode == "ab" and not live:
        mock_reports = [r for r in reports if r.backend == "mock"]
        all_ok = bool(mock_reports) and mock_reports[0].pass_rate.startswith("5/")

    print("ALL OK" if all_ok else "SOME FAILURES")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
