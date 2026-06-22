#!/usr/bin/env python3
"""Trace how Cursor conversation payloads are transformed and streamed."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from chat_fast import build_simple_chat_body, is_simple_qa, strip_agent_fields  # noqa: E402
from runtime_core.indexing_helpers import classify_message  # noqa: E402

ROUTER = "http://localhost:8080"
CAPTURE_DIR = ROOT / "tmp" / "cursor-captures"
OUT_JSON = ROOT / "tmp" / "conversation-flow-report.json"


def docker_logs(container: str, tail: int = 400) -> str:
    r = subprocess.run(
        ["docker", "logs", container, "--tail", str(tail)],
        capture_output=True,
        text=True,
        check=False,
    )
    return (r.stdout or "") + (r.stderr or "")


def summarize_body(body: dict[str, Any]) -> dict[str, Any]:
    msgs = body.get("messages", [])
    tools = body.get("tools", [])
    return {
        "message_count": len(msgs) if isinstance(msgs, list) else 0,
        "tool_count": len(tools) if isinstance(tools, list) else 0,
        "has_tools": bool(tools),
        "stream": bool(body.get("stream")),
        "max_tokens": body.get("max_tokens"),
        "roles": _role_counts(msgs),
        "message_previews": [
            {
                "role": m.get("role"),
                "chars": len(_text(m.get("content", ""))),
                "preview": _text(m.get("content", ""))[:120].replace("\n", " "),
            }
            for m in (msgs[:5] if isinstance(msgs, list) else [])
        ],
    }


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
        return "\n".join(parts)
    return str(content or "")


def _role_counts(msgs: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    if not isinstance(msgs, list):
        return out
    for m in msgs:
        if isinstance(m, dict):
            role = str(m.get("role", "unknown"))
            out[role] = out.get(role, 0) + 1
    return out


def classify_route(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    messages = body.get("messages", [])
    if isinstance(messages, list) and is_simple_qa(messages):
        transformed = build_simple_chat_body(body)
        strip_agent_fields(transformed)
        return "chat_fast", transformed
    return "passthrough", body


def analyze_capture(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    body = data.get("body", {})
    route, transformed = classify_route(body)
    return {
        "capture": path.name,
        "captured_at": data.get("captured_at"),
        "raw": summarize_body(body),
        "route": route,
        "transformed": summarize_body(transformed),
        "reduction_messages": summarize_body(body)["message_count"] - summarize_body(transformed)["message_count"],
        "tools_removed": summarize_body(body)["has_tools"] and not summarize_body(transformed)["has_tools"],
    }


def stream_profile(payload: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    chunks: list[str] = []
    ttft_ms = None
    done_ms = None
    text = ""
    usage: dict[str, Any] = {}

    with httpx.stream("POST", f"{ROUTER}/v1/chat/completions", json=payload, timeout=120.0) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            data_s = line[6:].strip()
            if data_s == "[DONE]":
                done_ms = (time.perf_counter() - t0) * 1000
                break
            try:
                chunk = json.loads(data_s)
            except json.JSONDecodeError:
                continue
            chunks.append(data_s[:120])
            if chunk.get("usage"):
                usage = chunk["usage"]
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content")
            if content:
                if ttft_ms is None:
                    ttft_ms = (time.perf_counter() - t0) * 1000
                text += content

    wall_ms = (time.perf_counter() - t0) * 1000
    return {
        "chunk_events": len(chunks),
        "ttft_ms": round(ttft_ms or 0, 1),
        "done_ms": round(done_ms or wall_ms, 1),
        "wall_ms": round(wall_ms, 1),
        "usage": usage,
        "preview": text[:200].replace("\n", " "),
    }


def parse_llama(text: str) -> dict[str, Any]:
    rows = []
    for m in re.finditer(
        r"prompt eval time =\s+([\d.]+) ms / \s*(\d+) tokens.*?eval time =\s+([\d.]+) ms / \s*(\d+) tokens",
        text,
        re.S,
    ):
        p_ms, p_n, e_ms, e_n = m.groups()
        rows.append(
            {
                "prompt_ms": float(p_ms),
                "prompt_n": int(p_n),
                "gen_ms": float(e_ms),
                "gen_n": int(e_n),
                "prompt_tps": int(p_n) / (float(p_ms) / 1000) if float(p_ms) else 0,
                "gen_tps": int(e_n) / (float(e_ms) / 1000) if float(e_ms) else 0,
            }
        )
    if not rows:
        return {"samples": 0}
    last = rows[-1]
    return {
        "samples": len(rows),
        "last": last,
        "avg_prompt_tps": round(sum(r["prompt_tps"] for r in rows) / len(rows), 1),
        "avg_gen_tps": round(sum(r["gen_tps"] for r in rows) / len(rows), 1),
    }


def main() -> int:
    print("=== 1) Cursor capture -> router transform ===")
    captures = sorted(CAPTURE_DIR.glob("*.request.json"))
    capture_reports = [analyze_capture(p) for p in captures[-3:]]
    for row in capture_reports:
        print(
            f"- {row['capture']}: raw {row['raw']['message_count']} msgs / "
            f"{row['raw']['tool_count']} tools -> route={row['route']} / "
            f"{row['transformed']['message_count']} msgs / tools={row['transformed']['has_tools']}"
        )

    test_queries = [
        ("simple_qa", "<user_query>\nstrawberry에 r 몇 개?\n</user_query>"),
        (
            "work_request",
            "<user_query>\n대화 내용 어떤식으로 출력되는지 스크립트 짜서 서버로그 확인하고 결과 벤치마킹 해봐\n</user_query>",
        ),
    ]

    print("\n=== 2) Route decision for representative queries ===")
    route_reports = []
    for name, query in test_queries:
        body = {
            "model": "model.gguf",
            "stream": True,
            "tools": [{"type": "function", "function": {"name": "Shell"}}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": query}]}],
        }
        route, transformed = classify_route(body)
        route_reports.append(
            {
                "name": name,
                "query": query,
                "route": route,
                "raw": summarize_body(body),
                "transformed": summarize_body(transformed),
            }
        )
        print(f"- {name}: route={route}, msgs={route_reports[-1]['transformed']['message_count']}, tools={route_reports[-1]['transformed']['has_tools']}")

    print("\n=== 3) Live stream output profile ===")
    stream_reports = []
    for name, query in test_queries:
        payload = {
            "model": "model.gguf",
            "stream": True,
            "max_tokens": 64,
            "tools": [{"type": "function", "function": {"name": "Shell"}}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": query}]}],
        }
        prof = stream_profile(payload)
        stream_reports.append({"name": name, **prof})
        print(
            f"- {name}: ttft={prof['ttft_ms']}ms done={prof['done_ms']}ms chunks={prof['chunk_events']} "
            f"preview={prof['preview'][:80]!r}"
        )

    router_log = docker_logs("cursor-local-llm-router", 200)
    llama_log = docker_logs("cursor-local-llm-fast", 300)
    router_stats = {
        "chat_fast": len(re.findall(r"chat_fast route=fast", router_log)),
        "lite_pack": len(re.findall(r"optimizer mode=lite_pack", router_log)),
        "http200": len(re.findall(r'POST /v1/chat/completions HTTP/1.1" 200', router_log)),
    }
    llama_stats = parse_llama(llama_log)

    print("\n=== 4) Server log snapshot ===")
    print(json.dumps({"router": router_stats, "llama_fast": llama_stats}, ensure_ascii=False, indent=2))

    report = {
        "captures": capture_reports,
        "routes": route_reports,
        "streams": stream_reports,
        "server_logs": {"router": router_stats, "llama_fast": llama_stats},
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved: {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
