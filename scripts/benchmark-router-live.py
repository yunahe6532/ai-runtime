#!/usr/bin/env python3
"""Live router benchmark: request timing + docker log analysis."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any

import httpx

ROUTER = "http://localhost:8080"
MODEL = "model.gguf"


@dataclass
class BenchResult:
    name: str
    route_hint: str
    stream: bool
    wall_ms: float
    ttft_ms: float | None
    stream_close_ms: float | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    gen_tps: float
    preview: str


def docker_logs(container: str, tail: int = 500) -> str:
    r = subprocess.run(
        ["docker", "logs", container, "--tail", str(tail)],
        capture_output=True,
        text=True,
        check=False,
    )
    return (r.stdout or "") + (r.stderr or "")


def parse_router_logs(text: str) -> dict[str, Any]:
    chat_fast = len(re.findall(r"chat_fast route=fast", text))
    lite_pack = len(re.findall(r"optimizer mode=lite_pack", text))
    route_fast = len(re.findall(r"route fast tokens=", text))
    route_long = len(re.findall(r"route long", text))
    http200 = len(re.findall(r"POST /v1/chat/completions HTTP/1.1\" 200", text))
    return {
        "chat_fast_requests": chat_fast,
        "lite_pack_requests": lite_pack,
        "route_fast": route_fast,
        "route_long": route_long,
        "chat_completions_200": http200,
    }


def parse_llama_timings(text: str, limit: int = 20) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for m in re.finditer(
        r"prompt eval time =\s+([\d.]+) ms / \s*(\d+) tokens.*?eval time =\s+([\d.]+) ms / \s*(\d+) tokens.*?total time =\s+([\d.]+) ms / \s*(\d+) tokens",
        text,
        re.S,
    ):
        prompt_ms, prompt_n, eval_ms, pred_n, total_ms, total_n = m.groups()
        rows.append(
            {
                "prompt_ms": float(prompt_ms),
                "prompt_n": float(prompt_n),
                "eval_ms": float(eval_ms),
                "pred_n": float(pred_n),
                "total_ms": float(total_ms),
                "total_n": float(total_n),
                "prompt_tps": float(prompt_n) / (float(prompt_ms) / 1000.0) if float(prompt_ms) else 0.0,
                "gen_tps": float(pred_n) / (float(eval_ms) / 1000.0) if float(eval_ms) else 0.0,
            }
        )
    return rows[-limit:]


def avg(rows: list[dict[str, float]], key: str) -> float:
    if not rows:
        return 0.0
    return sum(r[key] for r in rows) / len(rows)


def bench_non_stream(name: str, payload: dict[str, Any], route_hint: str) -> BenchResult:
    t0 = time.perf_counter()
    r = httpx.post(f"{ROUTER}/v1/chat/completions", json=payload, timeout=120.0)
    wall_ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    data = r.json()
    usage = data.get("usage", {})
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))
    total_tokens = int(usage.get("total_tokens", 0))
    gen_tps = completion_tokens / (wall_ms / 1000.0) if wall_ms > 0 and completion_tokens else 0.0
    preview = str(data.get("choices", [{}])[0].get("message", {}).get("content", ""))[:120].replace("\n", " ")
    return BenchResult(
        name=name,
        route_hint=route_hint,
        stream=False,
        wall_ms=wall_ms,
        ttft_ms=None,
        stream_close_ms=None,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        gen_tps=gen_tps,
        preview=preview,
    )


def bench_stream(name: str, payload: dict[str, Any], route_hint: str) -> BenchResult:
    t0 = time.perf_counter()
    ttft_ms: float | None = None
    close_ms: float | None = None
    text_parts: list[str] = []
    usage: dict[str, Any] = {}

    with httpx.stream("POST", f"{ROUTER}/v1/chat/completions", json=payload, timeout=120.0) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            data_s = line[6:].strip()
            if data_s == "[DONE]":
                close_ms = (time.perf_counter() - t0) * 1000
                break
            try:
                chunk = json.loads(data_s)
            except json.JSONDecodeError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content")
            if content:
                if ttft_ms is None:
                    ttft_ms = (time.perf_counter() - t0) * 1000
                text_parts.append(content)

    wall_ms = (time.perf_counter() - t0) * 1000
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))
    total_tokens = int(usage.get("total_tokens", 0))
    gen_tps = completion_tokens / (wall_ms / 1000.0) if wall_ms > 0 and completion_tokens else 0.0
    preview = "".join(text_parts)[:120].replace("\n", " ")
    return BenchResult(
        name=name,
        route_hint=route_hint,
        stream=True,
        wall_ms=wall_ms,
        ttft_ms=ttft_ms,
        stream_close_ms=close_ms or wall_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        gen_tps=gen_tps,
        preview=preview,
    )


def main() -> int:
    print("=== Router status ===")
    status = httpx.get(f"{ROUTER}/router/status", timeout=10.0).json()
    print(json.dumps(status, ensure_ascii=False, indent=2))

    cases = [
        (
            "chat_fast_simple",
            "chat_fast",
            {
                "model": MODEL,
                "stream": False,
                "max_tokens": 64,
                "tools": [{"type": "function", "function": {"name": "Shell"}}],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "<user_query>\n3.11이랑 3.9 중에 뭐가 더 크지?\n</user_query>"}
                        ],
                    }
                ],
            },
        ),
        (
            "chat_fast_stream",
            "chat_fast",
            {
                "model": MODEL,
                "stream": True,
                "max_tokens": 64,
                "tools": [{"type": "function", "function": {"name": "Shell"}}],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "<user_query>\nstrawberry에 r 몇 개?\n</user_query>"}
                        ],
                    }
                ],
            },
        ),
        (
            "agent_lite_pack",
            "lite_pack",
            {
                "model": MODEL,
                "stream": False,
                "max_tokens": 64,
                "tools": [{"type": "function", "function": {"name": "Shell", "description": "run"}}],
                "messages": [
                    {"role": "system", "content": "You are a coding assistant in Cursor. Use tools when needed."},
                    {"role": "user", "content": "<user_query>\nrouter 코드 수정해줘\n</user_query>"},
                ],
            },
        ),
        (
            "plain_small",
            "plain",
            {
                "model": MODEL,
                "stream": False,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "ping"}],
            },
        ),
    ]

    print("\n=== Live requests ===")
    results: list[BenchResult] = []
    for name, hint, payload in cases:
        try:
            if payload.get("stream"):
                res = bench_stream(name, payload, hint)
            else:
                res = bench_non_stream(name, payload, hint)
            results.append(res)
            print(
                f"- {name}: wall={res.wall_ms:.0f}ms prompt={res.prompt_tokens} completion={res.completion_tokens} "
                f"gen_tps={res.gen_tps:.1f} preview={res.preview!r}"
            )
            if res.ttft_ms is not None:
                print(f"  ttft={res.ttft_ms:.0f}ms stream_close={res.stream_close_ms:.0f}ms")
        except Exception as exc:
            print(f"- {name}: ERROR {exc}")

    router_log = docker_logs("cursor-local-llm-router", tail=300)
    llama_log = docker_logs("cursor-local-llm-fast", tail=400)
    router_stats = parse_router_logs(router_log)
    llama_rows = parse_llama_timings(llama_log, limit=10)

    print("\n=== Router log stats (tail 300) ===")
    print(json.dumps(router_stats, ensure_ascii=False, indent=2))

    print("\n=== llama-fast timing stats (recent) ===")
    if llama_rows:
        print(
            json.dumps(
                {
                    "samples": len(llama_rows),
                    "avg_prompt_ms": round(avg(llama_rows, "prompt_ms"), 1),
                    "avg_prompt_tokens": round(avg(llama_rows, "prompt_n"), 1),
                    "avg_prompt_tps": round(avg(llama_rows, "prompt_tps"), 1),
                    "avg_gen_tps": round(avg(llama_rows, "gen_tps"), 1),
                    "avg_total_ms": round(avg(llama_rows, "total_ms"), 1),
                    "last": llama_rows[-1],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print("no timing rows found")

    out = {
        "status": status,
        "live_results": [asdict(r) for r in results],
        "router_log_stats": router_stats,
        "llama_fast_recent": llama_rows[-5:],
    }
    out_path = "/home/yunahe/ai-runtime/cursor-local-llm/tmp/benchmark-router-live.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
