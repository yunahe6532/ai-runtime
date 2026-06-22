#!/usr/bin/env python3
"""Benchmark Qwen3.6 thinking modes for Runtime OS product metrics.

Compares:
  - preserve_thinking ON vs OFF
  - enable_thinking tool_planning ON vs OFF
  - native tool_call (qwen3_coder parser) vs XML fallback path

Usage:
  python3 scripts/benchmark-thinking-runtime.py
  python3 scripts/benchmark-thinking-runtime.py --url http://llama-long:8082
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    print("pip install httpx", file=sys.stderr)
    raise

RUNTIME = Path(__file__).resolve().parents[1]
OUT_DIR = RUNTIME / "tmp" / "benchmark-thinking"
OUT_DIR.mkdir(parents=True, exist_ok=True)

AGENT_SYSTEM = (
    "You are a coding agent. Use tools when needed. "
    "For file checks prefer Shell validation over repeated Read."
)

TOOL_DEF = [
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Shell",
            "description": "Run shell",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]


def run_case(
    client: httpx.Client,
    url: str,
    model: str,
    label: str,
    messages: list[dict],
    *,
    tools: list | None = None,
    chat_template_kwargs: dict | None = None,
    temperature: float = 0.6,
    top_p: float = 0.95,
    max_tokens: int = 512,
) -> dict:
    body: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if chat_template_kwargs:
        body["chat_template_kwargs"] = chat_template_kwargs

    t0 = time.perf_counter()
    r = client.post(f"{url}/v1/chat/completions", json=body, timeout=180.0)
    elapsed = time.perf_counter() - t0
    out: dict = {
        "label": label,
        "status": r.status_code,
        "elapsed_sec": round(elapsed, 3),
    }
    if r.status_code != 200:
        out["error"] = r.text[:500]
        return out

    data = r.json()
    usage = data.get("usage") or {}
    msg = data["choices"][0]["message"]
    content = str(msg.get("content") or "")
    reasoning = str(msg.get("reasoning_content") or "")
    tcs = msg.get("tool_calls") or []

    out.update(
        {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "has_tool_calls": bool(tcs),
            "tool_names": [
                tc.get("function", {}).get("name") for tc in tcs if isinstance(tc, dict)
            ],
            "content_chars": len(content),
            "reasoning_chars": len(reasoning),
            "thinking_in_content": (
                "think" in content.lower()
                or bool(reasoning.strip())
            ),
            "content_preview": content[:200].replace("\n", " "),
        }
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.getenv("LONG_URL", "http://localhost:8082"))
    ap.add_argument("--model", default="model.gguf")
    args = ap.parse_args()

    base_msgs = [
        {"role": "system", "content": AGENT_SYSTEM},
        {
            "role": "user",
            "content": "[Task]\nVISION.html 구조 검증. 이미 Read 했으면 Shell로 검증하라.",
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "tc1",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": json.dumps(
                            {"path": "/home/yunahe/ai-runtime/cursor-local-llm/docs/VISION.html"}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "Read",
            "content": "[artifact html] chars=53312 mermaid_blocks=16 html_parse_errors=0 validation_ok=true",
        },
    ]

    cases = [
        (
            "planning_preserve_on",
            {"enable_thinking": True, "preserve_thinking": True},
            True,
        ),
        (
            "planning_preserve_off",
            {"enable_thinking": True, "preserve_thinking": False},
            True,
        ),
        (
            "planning_thinking_off",
            {"enable_thinking": False},
            True,
        ),
        (
            "final_answer_no_think",
            {"enable_thinking": False, "preserve_thinking": False},
            False,
        ),
    ]

    results: list[dict] = []
    with httpx.Client() as client:
        try:
            client.get(f"{args.url}/v1/models", timeout=10.0)
        except httpx.HTTPError as exc:
            print(f"llama unreachable: {args.url} ({exc})", file=sys.stderr)
            return 1

        for label, ctk, with_tools in cases:
            msgs = list(base_msgs)
            if not with_tools:
                msgs = msgs + [
                    {
                        "role": "user",
                        "content": "검증 결과를 한국어로 요약해줘.",
                    }
                ]
            print(f"  run {label}...", flush=True)
            results.append(
                run_case(
                    client,
                    args.url,
                    args.model,
                    label,
                    msgs,
                    tools=TOOL_DEF if with_tools else None,
                    chat_template_kwargs=ctk,
                )
            )

    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = OUT_DIR / f"thinking-bench-{ts}.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Thinking Runtime Benchmark ===")
    print(f"url: {args.url}")
    for row in results:
        print(
            f"- {row['label']}: {row.get('status')} "
            f"{row.get('elapsed_sec')}s "
            f"tok={row.get('completion_tokens')} "
            f"tools={row.get('has_tool_calls')} "
            f"reasoning_chars={row.get('reasoning_chars', 0)}"
        )
    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
