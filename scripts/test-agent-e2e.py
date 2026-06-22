#!/usr/bin/env python3
"""Cursor-style multi-turn agent E2E: Read → Shell → Grep via router."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROUTER = "http://localhost:8080"
MODEL = "model.gguf"

TOOLS = [
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
            "description": "Run shell command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Grep",
            "description": "Search code",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
]

SYSTEM = (
    "You are a coding agent in Cursor (local LLM).\n"
    "- Use Read for files, Shell for commands, Grep for search.\n"
    "- Emit tool_calls only in tool planning phase.\n"
    "- Read files before answering from memory."
)


def post_chat(messages: list[dict], stream: bool = False, max_tokens: int = 400) -> tuple[dict, float]:
    body = {
        "model": MODEL,
        "stream": stream,
        "max_tokens": max_tokens,
        "tools": TOOLS,
        "messages": messages,
    }
    t0 = time.perf_counter()
    req = urllib.request.Request(
        f"{ROUTER}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.load(r)
    return data, (time.perf_counter() - t0) * 1000


def tool_calls_of(data: dict) -> list[dict]:
    msg = data.get("choices", [{}])[0].get("message", {})
    return msg.get("tool_calls") or []


def fake_tool_result(tool_call: dict, content: str) -> dict:
    fn = tool_call.get("function", {})
    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id", "call_fake"),
        "name": fn.get("name", "tool"),
        "content": content,
    }


def run_step(name: str, messages: list[dict], expect_tool: str | None) -> tuple[bool, list[dict], float]:
    data, wall_ms = post_chat(messages)
    tcs = tool_calls_of(data)
    names = [(tc.get("function") or {}).get("name") for tc in tcs]
    ok = True
    if expect_tool:
        ok = expect_tool in names
    print(f"  [{name}] wall={wall_ms:.0f}ms tools={names or '(none)'} finish={data['choices'][0].get('finish_reason')}")
    if tcs:
        for tc in tcs:
            args = (tc.get("function") or {}).get("arguments", "")
            print(f"    -> {tc['function']['name']}: {args[:100]}")
    return ok, tcs, wall_ms


def main() -> int:
    print("=== Cursor E2E: Read → Shell → Grep ===\n")

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": (
                "<user_query>\n"
                "router/intent_router.py 파일에서 route_backend 함수가 있는지 확인해줘. "
                "Read 도구로 파일을 읽어.\n"
                "</user_query>"
            ),
        },
    ]

    results: list[dict] = []

    # Step 1: file_read
    ok1, tcs1, w1 = run_step("file_read", messages, "Read")
    results.append({"step": "file_read", "pass": ok1, "wall_ms": w1})
    if not tcs1:
        print("\nFAIL: no Read tool_call in step 1")
        _save(results)
        return 1

    read_tc = tcs1[0]
    read_args = json.loads(read_tc["function"]["arguments"])
    fake_read = (
        "def route_backend(intent, score, files):\n"
        "    if intent in ('code_edit', 'debug'):\n"
        "        return 'main'\n"
        "    return 'fast'\n"
    )
    messages.append({"role": "assistant", "content": "", "tool_calls": tcs1})
    messages.append(fake_tool_result(read_tc, fake_read))

    # Step 2: Shell curl test
    messages.append({
        "role": "user",
        "content": (
            "<user_query>\n"
            "이제 curl로 http://localhost:8080/router/status 를 테스트해줘. Shell 도구 사용.\n"
            "</user_query>"
        ),
    })
    ok2, tcs2, w2 = run_step("shell_curl", messages, "Shell")
    results.append({"step": "shell_curl", "pass": ok2, "wall_ms": w2})
    if not tcs2:
        print("\nFAIL: no Shell tool_call in step 2")
        _save(results)
        return 1

    shell_tc = tcs2[0]
    messages.append({"role": "assistant", "content": "", "tool_calls": tcs2})
    messages.append(fake_tool_result(shell_tc, '{"active_backend":"long","exclusive":true}'))

    # Step 3: Grep
    messages.append({
        "role": "user",
        "content": (
            "<user_query>\n"
            "router/agent_exec.py에서 parse_function_xml 함수를 Grep으로 찾아줘.\n"
            "</user_query>"
        ),
    })
    ok3, tcs3, w3 = run_step("grep_parse", messages, "Grep")
    results.append({"step": "grep_parse", "pass": ok3, "wall_ms": w3})

    passed = sum(1 for r in results if r["pass"])
    total = len(results)
    print(f"\n=== E2E result: {passed}/{total} passed ===")
    _save(results)
    return 0 if passed == total else 1


def _save(results: list[dict]) -> None:
    out = Path(__file__).resolve().parents[1] / "tmp" / "agent-e2e-results.json"
    out.write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    print(f"Saved: {out}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:
        print(f"ERROR: router unreachable: {exc}")
        raise SystemExit(1) from exc
