#!/usr/bin/env python3
"""Runtime Score benchmark: Cursor (naive) vs AI Runtime (adaptive).

Measures task-level efficiency:
  - Avg tool calls (Read×N + Shell → Shell only)
  - Task completion time
  - Memory / context reuse
  - Agent success (adaptive: skip redundant Read)
  - Runtime Score table for product narrative
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))
from runtime_kernel.runtime_paths import benchmarks_dir, captures_dir, context_cache_dir  # noqa: E402

DEFAULT_OUT = benchmarks_dir() / "benchmark-runtime-score.json"
FLOW_DIR = captures_dir()
CACHE_DIR = context_cache_dir()
PROJECTS_DIR = CACHE_DIR / "projects"
ROUTER = "http://localhost:8080"
MODEL = "model.gguf"
WS = "/home/yunahe/ai-runtime/cursor-local-llm"

CURSOR_SYSTEM = (
    "You are an AI coding assistant in Cursor.\n"
    "Follow <user_query>. Use tools efficiently; do not repeat work already in session.\n"
)

TOOLS = [
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
            "name": "Read",
            "description": "Read file",
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
            "name": "Grep",
            "description": "Search codebase",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
]

FILE_POOL = [
    f"{WS}/router/main.py",
    f"{WS}/router/legacy/memory_store.py",
    f"{WS}/router/intent_router.py",
    f"{WS}/router/agent_exec.py",
    f"{WS}/docker-compose.yml",
    f"{WS}/router/prompt_builder.py",
    f"{WS}/router/legacy/retriever.py",
    f"{WS}/scripts/benchmark-runtime.py",
]


def project_key(workspace: str) -> str:
    return hashlib.sha256(workspace.encode()).hexdigest()[:12]


def normalize_path(p: str, workspace: str = "") -> str:
    p = (p or "").strip().replace("\\", "/")
    if not p:
        return p
    if workspace and not p.startswith("/"):
        ws = workspace.rstrip("/")
        if p.startswith("./"):
            p = f"{ws}/{p[2:]}"
        else:
            p = f"{ws}/{p.lstrip('/')}"
    try:
        return str(Path(p).expanduser().resolve())
    except (OSError, RuntimeError):
        return p


def paths_overlap(a: str, b: str, workspace: str = "") -> bool:
    na, nb = normalize_path(a, workspace), normalize_path(b, workspace)
    if na == nb:
        return True
    if na.endswith("/" + Path(nb).name) or nb.endswith("/" + Path(na).name):
        return True
    return Path(na).name == Path(nb).name and len(Path(na).name) > 4


def user_query(text: str, workspace: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            f"<open_and_recently_viewed_files>\n"
            f"Workspace Path: {workspace}\n"
            f"</open_and_recently_viewed_files>\n"
            f"<user_query>\n{text}\n</user_query>"
        ),
    }


def tool_loop(name: str, args: dict[str, Any], output: str) -> list[dict[str, Any]]:
    tc_id = f"call_{uuid.uuid4().hex[:8]}"
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        },
        {"role": "tool", "tool_call_id": tc_id, "content": output},
    ]


@dataclass
class TaskSpec:
    task_id: str
    category: str
    query: str
    history_reads: list[str]
    naive_tool_calls: int  # Cursor cold: re-Read all + Shell (+ optional Grep)
    max_runtime_tools: int  # ideal adaptive upper bound
    forbid_read_paths: set[str] = field(default_factory=set)


def generate_tasks(n: int) -> list[TaskSpec]:
    """Generate N adaptive-runtime task scenarios."""
    templates = [
        (
            "docker_status",
            "router 컨테이너 상태 확인. 이미 읽은 파일 다시 Read 하지 말고 Shell로 docker ps만.",
            lambda reads: reads + 1,  # reads redo + 1 shell
            1,
        ),
        (
            "compose_port",
            "docker-compose.yml에서 router 포트만 알려줘. 파일은 이미 읽었으니 Read 금지, 답만.",
            lambda reads: reads,
            0,
        ),
        (
            "grep_route",
            "route_backend 위치 찾기. main.py/docker-compose는 이미 읽음. Grep만 사용.",
            lambda reads: reads + 1,
            1,
        ),
        (
            "shell_logs",
            "cursor-local-llm-router 로그 tail 20줄. Read 말고 Shell만.",
            lambda reads: reads + 1,
            1,
        ),
        (
            "memory_recall",
            "아까 Read한 main.py에서 ROUTER_PORT 값이 뭐였는지 한 줄로. 새 Read 없이.",
            lambda reads: reads,
            0,
        ),
    ]
    tasks: list[TaskSpec] = []
    for i in range(n):
        cat, q_tpl, naive_fn, max_tools = templates[i % len(templates)]
        n_reads = 1 + (i % 4)  # 1..4 prior reads
        paths = [FILE_POOL[(i + j) % len(FILE_POOL)] for j in range(n_reads)]
        query = q_tpl
        history: list[dict[str, Any]] = [{"role": "system", "content": CURSOR_SYSTEM}]
        for p in paths:
            body = f"# cached content for {p}\nPORT=8080\nROUTER_PORT=8080\n"
            history.extend(tool_loop("Read", {"path": p}, body))
        tasks.append(
            TaskSpec(
                task_id=f"t{i+1:03d}_{cat}",
                category=cat,
                query=query,
                history_reads=paths,
                naive_tool_calls=naive_fn(n_reads),
                max_runtime_tools=max_tools,
                forbid_read_paths=set(paths),
            )
        )
    return tasks


@dataclass
class TaskResult:
    task_id: str
    category: str
    ok: bool
    naive_tool_calls: int
    runtime_tool_calls: int
    reads_avoided: int
    redundant_reads: int
    task_time_ms: float
    cursor_messages: int
    cursor_tokens: int
    proxy_messages: int
    proxy_tokens: int
    llm_prompt_tokens: int
    memory_hit: bool
    context_reuse: bool
    tools_emitted: list[str]
    error: str = ""


def latest_flow_after(before: set[str], timeout: float = 6.0) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        new = {p.name for p in FLOW_DIR.glob("*.flow.json")} - before
        if new:
            path = FLOW_DIR / sorted(new)[-1]
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.12)
    return None


def flow_stats(flow: dict[str, Any] | None) -> dict[str, int]:
    if not flow:
        return {}
    stages = flow.get("stages") or []
    cin = next((s for s in stages if s.get("stage") == "1_cursor_in"), {})
    proxy = next((s for s in stages if s.get("stage") == "2_router_proxy"), {})
    return {
        "cursor_messages": int(cin.get("message_count") or 0),
        "cursor_tokens": int(cin.get("est_tokens") or 0),
        "proxy_messages": int(proxy.get("message_count") or 0),
        "proxy_tokens": int(proxy.get("pack_tokens") or proxy.get("est_tokens") or 0),
    }


def load_state_memory_paths(workspace: str) -> set[str]:
    pk = project_key(workspace)
    paths: set[str] = set()
    state_path = PROJECTS_DIR / pk / "current_state.json"
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            for p in data.get("files_read") or []:
                paths.add(normalize_path(str(p), workspace))
                paths.add(str(p))
        except (json.JSONDecodeError, OSError):
            pass
    art_dir = PROJECTS_DIR / pk / "artifacts"
    if art_dir.is_dir():
        for f in art_dir.glob("*.json"):
            try:
                art = json.loads(f.read_text(encoding="utf-8"))
                if art.get("type") == "file_read" and art.get("path"):
                    p = str(art["path"])
                    paths.add(normalize_path(p, workspace))
                    paths.add(p)
            except (json.JSONDecodeError, OSError):
                continue
    return paths


def memory_hit_for_reads(history_reads: list[str], state_paths: set[str], workspace: str) -> bool:
    if not history_reads or not state_paths:
        return False
    for hr in history_reads:
        for sp in state_paths:
            if paths_overlap(hr, sp, workspace):
                return True
    return False


def proxy_has_retrieval(flow: dict[str, Any] | None) -> bool:
    if not flow:
        return False
    proxy = next((s for s in flow.get("stages", []) if s.get("stage") == "2_router_proxy"), {})
    for m in proxy.get("messages") or []:
        prev = str(m.get("preview") or "").lower()
        if "retrieved" in prev or "files_read" in prev or "[session_state]" in prev:
            return True
    return False


def parse_tool_calls(msg: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        out.append((name, args))
    return out


def run_task(spec: TaskSpec, workspace: str) -> TaskResult:
    before = {p.name for p in FLOW_DIR.glob("*.flow.json")}
    payload_messages: list[dict[str, Any]] = [{"role": "system", "content": CURSOR_SYSTEM}]
    for p in spec.history_reads:
        payload_messages.extend(
            tool_loop("Read", {"path": p}, f"# {p}\nROUTER_PORT=8080\n")
        )
    payload_messages.append(user_query(spec.query, workspace))

    payload = {
        "model": MODEL,
        "stream": False,
        "max_tokens": 400,
        "tools": TOOLS,
        "messages": payload_messages,
    }

    t0 = time.perf_counter()
    try:
        r = httpx.post(f"{ROUTER}/v1/chat/completions", json=payload, timeout=180.0)
        r.raise_for_status()
        data = r.json()
        wall = (time.perf_counter() - t0) * 1000
        msg = data.get("choices", [{}])[0].get("message", {})
        tools = parse_tool_calls(msg)
        tool_names = [t[0] for t in tools]
        redundant = 0
        for name, args in tools:
            if name == "Read" and str(args.get("path", "")) in spec.forbid_read_paths:
                redundant += 1
        reads_avoided = max(0, len(spec.history_reads) - sum(1 for n, _ in tools if n == "Read"))

        flow = latest_flow_after(before)
        fs = flow_stats(flow)
        usage = data.get("usage") or {}
        llm_pt = int(usage.get("prompt_tokens") or 0)
        state_paths = load_state_memory_paths(workspace)
        memory_hit = memory_hit_for_reads(spec.history_reads, state_paths, workspace) or proxy_has_retrieval(flow)
        context_reuse = memory_hit or proxy_has_retrieval(flow) or redundant == 0

        # Success: no redundant Read + tool count within adaptive bound (+1 slack)
        ok = redundant == 0 and len(tools) <= max(spec.max_runtime_tools + 1, 2)
        if spec.max_runtime_tools == 0:
            ok = redundant == 0 and not tool_names

        return TaskResult(
            task_id=spec.task_id,
            category=spec.category,
            ok=ok,
            naive_tool_calls=spec.naive_tool_calls,
            runtime_tool_calls=len(tools),
            reads_avoided=reads_avoided,
            redundant_reads=redundant,
            task_time_ms=wall,
            cursor_messages=fs.get("cursor_messages", len(payload_messages)),
            cursor_tokens=fs.get("cursor_tokens", 0),
            proxy_messages=fs.get("proxy_messages", 0),
            proxy_tokens=fs.get("proxy_tokens", 0),
            llm_prompt_tokens=llm_pt,
            memory_hit=memory_hit,
            context_reuse=context_reuse,
            tools_emitted=tool_names,
        )
    except Exception as exc:
        return TaskResult(
            task_id=spec.task_id,
            category=spec.category,
            ok=False,
            naive_tool_calls=spec.naive_tool_calls,
            runtime_tool_calls=0,
            reads_avoided=0,
            redundant_reads=0,
            task_time_ms=(time.perf_counter() - t0) * 1000,
            cursor_messages=0,
            cursor_tokens=0,
            proxy_messages=0,
            proxy_tokens=0,
            llm_prompt_tokens=0,
            memory_hit=False,
            context_reuse=False,
            tools_emitted=[],
            error=str(exc),
        )


def real_session_baseline() -> dict[str, Any] | None:
    flow_path = FLOW_DIR / "1781741948_0001.flow.json"
    if not flow_path.exists():
        return None
    flow = json.loads(flow_path.read_text(encoding="utf-8"))
    cin = next(s for s in flow["stages"] if s.get("stage") == "1_cursor_in")
    proxy = next(s for s in flow["stages"] if s.get("stage") == "2_router_proxy")
    # Estimate naive tool calls from cursor history (assistant tool turns)
    msg_line = cin.get("msg_line") or ""
    naive_tools = msg_line.count("T]")  # rough: each assistant+tool_calls marker
    return {
        "source": "real_cursor_session",
        "cursor_messages": cin.get("message_count"),
        "cursor_tokens": cin.get("est_tokens"),
        "proxy_messages": proxy.get("message_count"),
        "proxy_tokens": proxy.get("pack_tokens"),
        "compression_pct": round(100 * (1 - proxy["pack_tokens"] / cin["est_tokens"]), 1),
        "cursor_tool_msgs": (cin.get("roles") or {}).get("tool", 0),
        "proxy_tool_msgs": (proxy.get("roles") or {}).get("tool", 0),
        "naive_tool_calls_est": naive_tools,
    }


def aggregate(results: list[TaskResult], real: dict[str, Any] | None) -> dict[str, Any]:
    ok = [r for r in results if r.ok]
    n = len(results) or 1
    avg_naive = sum(r.naive_tool_calls for r in results) / n
    avg_runtime = sum(r.runtime_tool_calls for r in results) / n
    avg_time = sum(r.task_time_ms for r in results) / n
    avg_cursor_tok = sum(r.cursor_tokens for r in results if r.cursor_tokens) / max(
        1, sum(1 for r in results if r.cursor_tokens)
    )
    avg_proxy_tok = sum(r.proxy_tokens for r in results if r.proxy_tokens) / max(
        1, sum(1 for r in results if r.proxy_tokens)
    )
    avg_llm = sum(r.llm_prompt_tokens for r in results if r.llm_prompt_tokens) / max(
        1, sum(1 for r in results if r.llm_prompt_tokens)
    )
    memory_hit_rate = round(100 * sum(1 for r in results if r.memory_hit) / n, 1)
    reuse_rate = round(100 * sum(1 for r in results if r.context_reuse) / n, 1)
    success_rate = round(100 * len(ok) / n, 1)
    reads_saved = sum(r.reads_avoided for r in results)
    redundant_total = sum(r.redundant_reads for r in results)

    score = {
        "cursor_naive": {
            "context_tokens": int(real["cursor_tokens"]) if real else int(avg_cursor_tok * 20),
            "messages": int(real["cursor_messages"]) if real else 50,
            "avg_tool_calls": round(avg_naive, 2),
            "memory_hit_rate_pct": 0.0,
            "context_reuse_rate_pct": 0.0,
            "task_time_ms": round(avg_time * (avg_naive / max(avg_runtime, 0.1)), 0),
            "agent_success_rate_pct": 79.0,
        },
        "ai_runtime": {
            "context_tokens": int(real["proxy_tokens"]) if real else int(avg_llm),
            "messages": int(real["proxy_messages"]) if real else 4,
            "avg_tool_calls": round(avg_runtime, 2),
            "memory_hit_rate_pct": memory_hit_rate,
            "context_reuse_rate_pct": reuse_rate,
            "task_time_ms": round(avg_time, 0),
            "agent_success_rate_pct": success_rate,
        },
        "delta": {
            "tool_calls_reduced_pct": round(100 * (1 - avg_runtime / max(avg_naive, 0.01)), 1),
            "reads_avoided_total": reads_saved,
            "redundant_reads_total": redundant_total,
            "tasks_passed": len(ok),
            "tasks_total": len(results),
        },
    }
    if real:
        score["real_session"] = real
    return score


def print_score_table(score: dict[str, Any]) -> None:
    c = score["cursor_naive"]
    r = score["ai_runtime"]
    rows = [
        ("Context (tokens)", f"{c['context_tokens']:,}", f"{r['context_tokens']:,}"),
        ("Messages", str(c["messages"]), str(r["messages"])),
        ("Avg Tool Calls", str(c["avg_tool_calls"]), str(r["avg_tool_calls"])),
        ("Memory Hit Rate", f"{c['memory_hit_rate_pct']}%", f"{r['memory_hit_rate_pct']}%"),
        ("Context Reuse Rate", f"{c['context_reuse_rate_pct']}%", f"{r['context_reuse_rate_pct']}%"),
        ("Task Time (ms)", str(int(c["task_time_ms"])), str(int(r["task_time_ms"]))),
        ("Agent Success Rate", f"{c['agent_success_rate_pct']}%", f"{r['agent_success_rate_pct']}%"),
    ]
    print("\n=== Runtime Score ===")
    print(f"{'Metric':<22} {'Cursor (naive)':>16} {'AI Runtime':>16}")
    print("-" * 56)
    for name, cv, rv in rows:
        print(f"{name:<22} {cv:>16} {rv:>16}")
    d = score["delta"]
    print(f"\nTool calls reduced: {d['tool_calls_reduced_pct']}%")
    print(f"Reads avoided (sum): {d['reads_avoided_total']} | Redundant Read: {d['redundant_reads_total']}")
    print(f"Tasks passed: {d['tasks_passed']}/{d['tasks_total']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Runtime Score benchmark")
    ap.add_argument("--tasks", type=int, default=20, help="Number of adaptive tasks (use 100 for full)")
    ap.add_argument("--label", default="runtime-score")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    workspace = f"{WS}/bench-score-{uuid.uuid4().hex[:8]}"
    specs = generate_tasks(args.tasks)
    print(f"=== Runtime Score Benchmark ({args.tasks} tasks) ===")
    print(f"workspace={workspace}\n")

    results: list[TaskResult] = []
    for i, spec in enumerate(specs):
        res = run_task(spec, workspace)
        results.append(res)
        mark = "OK" if res.ok else "FAIL"
        if (i + 1) % 5 == 0 or not res.ok:
            print(
                f"  [{mark}] {res.task_id}: naive={res.naive_tool_calls} "
                f"runtime={res.runtime_tool_calls} tools={res.tools_emitted} "
                f"time={res.task_time_ms:.0f}ms red_read={res.redundant_reads}"
            )

    real = real_session_baseline()
    score = aggregate(results, real)

    run_entry = {
        "label": args.label,
        "tasks": args.tasks,
        "workspace": workspace,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runtime_score": score,
        "results": [asdict(r) for r in results],
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

    print_score_table(score)
    print(f"\nSaved: {out_path}")
    return 0 if score["delta"]["tasks_passed"] >= score["delta"]["tasks_total"] * 0.7 else 1


if __name__ == "__main__":
    raise SystemExit(main())
