#!/usr/bin/env python3
"""Runtime benchmark: context compression, memory ON/OFF, multi-turn, task proxy efficiency.

Measures what the LLM benchmark cannot:
  - Cursor history tokens vs proxy tokens sent to llama
  - Memory ON vs passthrough pack
  - Multi-turn growth (10 / 50 / 100 synthetic turns)
  - Tool-result retention in proxy (agent sessions)
"""

from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tmp" / "benchmark-runtime.json"
FLOW_DIR = ROOT / "tmp" / "cursor-captures"
ROUTER = "http://localhost:8080"
MODEL = "model.gguf"
WS = "/home/yunahe/ai-runtime/cursor-local-llm"

CURSOR_SYSTEM = (
    "You are an AI coding assistant, powered by model.gguf.\n"
    "You operate in Cursor.\n"
    "Your main goal is to follow the USER's instructions in <user_query>.\n"
)


def est_tokens(obj: Any) -> int:
    return max(1, len(json.dumps(obj, ensure_ascii=False)) // 3)


def user_query(text: str, workspace: str | None = None) -> dict[str, Any]:
    parts: list[str] = []
    if workspace:
        parts.append(
            f"<open_and_recently_viewed_files>\n"
            f"Workspace Path: {workspace}\n"
            f"</open_and_recently_viewed_files>"
        )
    parts.append(f"<user_query>\n{text}\n</user_query>")
    return {"role": "user", "content": "\n".join(parts)}


def filler_block(n: int, size: int) -> str:
    base = f"[synthetic turn {n}] " + ("x" * max(0, size - 24))
    return base[:size]


def build_turn_pair(turn: int, filler_size: int) -> list[dict[str, Any]]:
    return [
        user_query(filler_block(turn, filler_size), workspace=None),
        {"role": "assistant", "content": f"Acknowledged turn {turn}. (synthetic)"},
    ]


def build_tool_loop(file_path: str, tool_output: str) -> list[dict[str, Any]]:
    tc_id = f"call_{uuid.uuid4().hex[:8]}"
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": json.dumps({"path": file_path}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": tc_id, "content": tool_output},
    ]


def build_agent_history(
    *,
    turns: int,
    filler_size: int,
    tool_loops: int,
    workspace: str | None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": CURSOR_SYSTEM}]
    for t in range(1, turns + 1):
        messages.extend(build_turn_pair(t, filler_size))
    files = [
        f"{WS}/router/main.py",
        f"{WS}/docker-compose.yml",
        f"{WS}/router/legacy/memory_store.py",
        f"{WS}/router/intent_router.py",
    ]
    for i in range(tool_loops):
        path = files[i % len(files)]
        content = f"# file {path}\n" + ("log line\n" * 80) + f"chunk {i}\n"
        messages.extend(build_tool_loop(path, content))
    messages.append(
        user_query(
            "router가 왜 unhealthy인지 분석해줘. 이미 읽은 파일은 다시 Read 하지 말고 Shell로 docker ps만 확인해.",
            workspace=workspace,
        )
    )
    return messages


def latest_flow_after(before: set[str], timeout: float = 5.0) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        after = {p.name for p in FLOW_DIR.glob("*.flow.json")}
        new = after - before
        if new:
            path = FLOW_DIR / sorted(new)[-1]
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.15)
    return None


def flow_metrics(flow: dict[str, Any] | None) -> dict[str, Any]:
    if not flow:
        return {}
    stages = flow.get("stages") or []
    cin = next((s for s in stages if s.get("stage") == "1_cursor_in"), {})
    proxy = next((s for s in stages if s.get("stage") == "2_router_proxy"), {})
    resp = next((s for s in stages if s.get("stage") == "3_llm_response"), {})
    cursor_tok = int(cin.get("est_tokens") or 0)
    proxy_tok = int(proxy.get("pack_tokens") or proxy.get("est_tokens") or 0)
    llm_prompt = int(resp.get("prompt_tokens") or 0)
    compression = round(100 * (1 - proxy_tok / cursor_tok), 1) if cursor_tok else 0.0
    cursor_roles = cin.get("roles") or {}
    proxy_roles = proxy.get("roles") or {}
    return {
        "flow_id": flow.get("id"),
        "cursor_messages": int(cin.get("message_count") or 0),
        "cursor_est_tokens": cursor_tok,
        "cursor_tool_msgs": int(cursor_roles.get("tool", 0)),
        "proxy_messages": int(proxy.get("message_count") or 0),
        "proxy_est_tokens": proxy_tok,
        "proxy_tool_msgs": int(proxy_roles.get("tool", 0)),
        "compression_pct": compression,
        "llm_prompt_tokens": llm_prompt,
        "intent": proxy.get("intent"),
        "phase": proxy.get("phase"),
        "memory_saved_pct": float(proxy.get("saved_pct") or 0),
        "tool_calls_emitted": len(resp.get("sent_tool_calls") or resp.get("raw_tool_calls") or []),
    }


@dataclass
class RuntimeCase:
    name: str
    ok: bool
    wall_ms: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def post_chat(payload: dict[str, Any], timeout: float = 180.0) -> tuple[dict[str, Any], float]:
    t0 = time.perf_counter()
    r = httpx.post(f"{ROUTER}/v1/chat/completions", json=payload, timeout=timeout)
    wall = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    return r.json(), wall


def run_case(
    name: str,
    payload: dict[str, Any],
    before_flows: set[str],
) -> RuntimeCase:
    try:
        data, wall = post_chat(payload)
        flow = latest_flow_after(before_flows)
        m = flow_metrics(flow)
        usage = data.get("usage") or {}
        m["llm_prompt_tokens"] = int(usage.get("prompt_tokens") or m.get("llm_prompt_tokens") or 0)
        m["llm_completion_tokens"] = int(usage.get("completion_tokens") or 0)
        msg = data.get("choices", [{}])[0].get("message", {})
        tcs = msg.get("tool_calls") or []
        m["response_tool_calls"] = [tc.get("function", {}).get("name") for tc in tcs if isinstance(tc, dict)]
        return RuntimeCase(name=name, ok=True, wall_ms=wall, metrics=m)
    except Exception as exc:
        return RuntimeCase(name=name, ok=False, error=str(exc))


def analyze_saved_flow(path: Path) -> dict[str, Any]:
    flow = json.loads(path.read_text(encoding="utf-8"))
    m = flow_metrics(flow)
    m["source"] = str(path)
    m["label"] = "real_cursor_capture"
    return m


def summarize(cases: list[RuntimeCase], extras: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [c for c in cases if c.ok]
    compressions = [c.metrics.get("compression_pct", 0) for c in ok if c.metrics.get("cursor_est_tokens")]
    proxy_tokens = [c.metrics.get("llm_prompt_tokens", 0) for c in ok if c.metrics.get("llm_prompt_tokens")]
    return {
        "total_cases": len(cases),
        "passed": len(ok),
        "avg_compression_pct": round(sum(compressions) / len(compressions), 1) if compressions else 0,
        "avg_llm_prompt_tokens": round(sum(proxy_tokens) / len(proxy_tokens), 0) if proxy_tokens else 0,
        "extras": extras,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="AI Runtime benchmark (memory / context / task proxy)")
    ap.add_argument("--label", default="runtime")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--quick", action="store_true", help="Skip long 100-turn case")
    args = ap.parse_args()

    if not FLOW_DIR.is_dir():
        print(f"WARN: flow dir missing: {FLOW_DIR} (FLOW_TRACE=1 필요)")
    before_all = {p.name for p in FLOW_DIR.glob("*.flow.json")}

    cases: list[RuntimeCase] = []
    extras: list[dict[str, Any]] = []

    # --- Real capture baseline (289 msgs) ---
    real_flow = FLOW_DIR / "1781741948_0001.flow.json"
    if real_flow.exists():
        extras.append(analyze_saved_flow(real_flow))

    # --- Context scale: synthetic turns (memory ON) ---
    bench_ws = f"{WS}/bench-runtime-{uuid.uuid4().hex[:8]}"
    scale_specs = [
        ("scale_10_turns", 10, 400),
        ("scale_50_turns", 50, 400),
    ]
    if not args.quick:
        scale_specs.append(("scale_100_turns", 100, 400))

    for name, turns, filler in scale_specs:
        before = {p.name for p in FLOW_DIR.glob("*.flow.json")}
        payload = {
            "model": MODEL,
            "stream": False,
            "max_tokens": 200,
            "tools": [],
            "messages": build_agent_history(
                turns=turns,
                filler_size=filler,
                tool_loops=0,
                workspace=bench_ws,
            ),
        }
        payload["messages"][-1] = user_query(
            f"지금까지 {turns}턴 대화 요약 한 줄.",
            workspace=bench_ws,
        )
        c = run_case(name, payload, before)
        cases.append(c)
        print(
            f"  [{'OK' if c.ok else 'FAIL'}] {name}: "
            f"cursor={c.metrics.get('cursor_est_tokens')} → proxy={c.metrics.get('proxy_est_tokens')} "
            f"({c.metrics.get('compression_pct')}%) llm_in={c.metrics.get('llm_prompt_tokens')} "
            f"err={c.error}"
        )

    # --- Memory ON vs OFF (same history size) ---
    mem_ws = f"{WS}/bench-mem-{uuid.uuid4().hex[:8]}"
    for suffix, workspace in (("memory_on", mem_ws), ("memory_off", None)):
        before = {p.name for p in FLOW_DIR.glob("*.flow.json")}
        payload = {
            "model": MODEL,
            "stream": False,
            "max_tokens": 300,
            "tools": [],
            "messages": build_agent_history(
                turns=20,
                filler_size=600,
                tool_loops=3,
                workspace=workspace,
            ),
        }
        c = run_case(f"mem_compare_{suffix}", payload, before)
        cases.append(c)
        print(
            f"  [{'OK' if c.ok else 'FAIL'}] mem_compare_{suffix}: "
            f"cursor={c.metrics.get('cursor_est_tokens')} proxy={c.metrics.get('proxy_est_tokens')} "
            f"tool_in_proxy={c.metrics.get('proxy_tool_msgs')}/{c.metrics.get('cursor_tool_msgs')}"
        )

    # --- Task proxy: agent with heavy history (tool loop efficiency) ---
    task_ws = f"{WS}/bench-task-{uuid.uuid4().hex[:8]}"
    before = {p.name for p in FLOW_DIR.glob("*.flow.json")}
    tools = [
        {
            "type": "function",
            "function": {
                "name": "Shell",
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
                "name": "Read",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
    ]
    payload = {
        "model": MODEL,
        "stream": False,
        "max_tokens": 400,
        "tools": tools,
        "messages": build_agent_history(
            turns=5,
            filler_size=300,
            tool_loops=8,
            workspace=task_ws,
        ),
    }
    c = run_case("task_agent_proxy", payload, before)
    cases.append(c)
    rtc = c.metrics.get("response_tool_calls") or []
    print(
        f"  [{'OK' if c.ok else 'FAIL'}] task_agent_proxy: "
        f"history_tools={c.metrics.get('cursor_tool_msgs')} proxy_tools={c.metrics.get('proxy_tool_msgs')} "
        f"emitted={rtc} wall={c.wall_ms:.0f}ms"
    )

    # --- Multi-turn accumulation (same project, 5 sequential) ---
    acc_ws = f"{WS}/bench-acc-{uuid.uuid4().hex[:8]}"
    acc_metrics: list[dict[str, Any]] = []
    acc_messages: list[dict[str, Any]] = [{"role": "system", "content": CURSOR_SYSTEM}]
    for i in range(1, 6):
        acc_messages.append(user_query(f"누적 턴 {i}: router 상태 한 줄.", workspace=acc_ws))
        before = {p.name for p in FLOW_DIR.glob("*.flow.json")}
        payload = {
            "model": MODEL,
            "stream": False,
            "max_tokens": 80,
            "messages": list(acc_messages),
        }
        try:
            data, wall = post_chat(payload)
            flow = latest_flow_after(before)
            m = flow_metrics(flow)
            m["turn"] = i
            m["wall_ms"] = wall
            m["llm_prompt_tokens"] = int((data.get("usage") or {}).get("prompt_tokens") or 0)
            acc_metrics.append(m)
            acc_messages.append(
                {
                    "role": "assistant",
                    "content": str(data.get("choices", [{}])[0].get("message", {}).get("content") or "ok"),
                }
            )
        except Exception as exc:
            acc_metrics.append({"turn": i, "error": str(exc)})
    extras.append({"multi_turn_accumulation": acc_metrics})
    if acc_metrics:
        print(
            f"  [ACC] turns 1→5 llm_prompt: "
            + " → ".join(str(m.get("llm_prompt_tokens", "?")) for m in acc_metrics)
        )

    summary = summarize(cases, extras)
    run_entry = {
        "label": args.label,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": summary,
        "cases": [asdict(c) for c in cases],
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {"runs": []}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    existing.setdefault("runs", []).append(run_entry)
    out_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Runtime Benchmark Summary ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nSaved: {out_path}")
    return 0 if summary["passed"] == summary["total_cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
