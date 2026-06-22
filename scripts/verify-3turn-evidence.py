#!/usr/bin/env python3
"""3-turn benchmark evidence E2E verification."""
from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path("/home/yunahe/ai-runtime/cursor-local-llm")
API = "http://localhost:8080/v1/chat/completions"
RUNS = "http://localhost:8080/router/agent/runs"
QUERY = "벤치마크 분석: runtime score, agent benchmark, flow phase를 순서대로 확인하고 요약해줘"
SCORE = (ROOT / "tmp/benchmark-runtime-score.json").read_text()
AGENT = (ROOT / "tmp/benchmark-cursor-agent.json").read_text()
FLOW = (ROOT / "tmp/cursor-captures/1781758111_0037.flow.json").read_text()
TOOLS = [
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
    }
]


def post(messages: list[dict]) -> dict:
    payload = {"model": "model.gguf", "stream": False, "tools": TOOLS, "messages": messages}
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def latest_run() -> dict:
    with urllib.request.urlopen(RUNS) as r:
        runs = json.loads(r.read())["runs"]
    rid = runs[0]["run_id"]
    with urllib.request.urlopen(f"{RUNS}/{rid}") as r:
        return json.loads(r.read())


def chain_runs() -> list[dict]:
    with urllib.request.urlopen(RUNS) as r:
        runs = json.loads(r.read())["runs"]
    # oldest first within last 3
    ids = [runs[i]["run_id"] for i in range(min(3, len(runs)))]
    ids.reverse()
    out = []
    for rid in ids:
        with urllib.request.urlopen(f"{RUNS}/{rid}") as r:
            out.append(json.loads(r.read()))
    return out


def show(label: str, run: dict) -> None:
    ev = [e for e in run["events"] if e.get("status") == "evidence.collected"]
    fr = [e for e in run["events"] if e.get("status") == "final.ready"]
    print(f"\n=== {label} ===")
    print(f"run_id={run['run_id']} turn={run.get('turn_index')} parent={run.get('parent_run_id')}")
    print(f"evidence.collected x{len(ev)}: {[e.get('text') for e in ev]}")
    print(f"final.ready x{len(fr)}: {[e.get('text') for e in fr]}")
    # plan evidence from state file if available
    state_path = ROOT / "tmp/context-cache/current_state.json"
    if state_path.exists():
        st = json.loads(state_path.read_text())
        ap = st.get("agent_plan") or {}
        print(f"plan.evidence_collected={ap.get('evidence_collected', [])}")
        print(f"plan.next_action={ap.get('next_action', {})}")


def main() -> None:
    # reset session for clean chain
    state_file = ROOT / "tmp/context-cache/current_state.json"
    if state_file.exists():
        state_file.unlink()

    base = [{"role": "user", "content": QUERY}]

    # Turn 1: runtime score tool result
    m1 = base + [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": json.dumps({"path": str(ROOT / "tmp/benchmark-runtime-score.json")}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "Read", "content": SCORE},
    ]
    r1 = post(m1)
    print("TURN 1 response:", r1["choices"][0]["finish_reason"], "tools=", [t["function"]["name"] for t in r1["choices"][0]["message"].get("tool_calls") or []])
    show("after turn 1", latest_run())

    # Turn 2: + agent benchmark
    m2 = m1 + [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c2",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": json.dumps({"path": str(ROOT / "tmp/benchmark-cursor-agent.json")}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c2", "name": "Read", "content": AGENT},
    ]
    r2 = post(m2)
    print("TURN 2 response:", r2["choices"][0]["finish_reason"])
    show("after turn 2", latest_run())

    # Turn 3: + flow
    m3 = m2 + [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c3",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": json.dumps({"path": str(ROOT / "tmp/cursor-captures/1781758111_0037.flow.json")}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c3", "name": "Read", "content": FLOW},
    ]
    r3 = post(m3)
    msg = r3["choices"][0]["message"]
    print("TURN 3 response:", r3["choices"][0]["finish_reason"], "content_len=", len(msg.get("content") or ""))
    run3 = latest_run()
    show("after turn 3", run3)

    chain = chain_runs()
    ev_total = sum(
        1 for run in chain for e in run["events"] if e.get("status") == "evidence.collected"
    )
    fr_count = sum(
        1 for run in chain for e in run["events"] if e.get("status") == "final.ready"
    )
    finished = all(run.get("status") == "finished" for run in chain)
    ok = ev_total >= 3 and fr_count >= 1 and finished and r3["choices"][0]["finish_reason"] == "stop"
    print("\n=== VERDICT ===")
    print(f"evidence.collected events (chain): {ev_total} (need >=3)")
    print(f"final.ready events (chain): {fr_count} (need >=1)")
    print(f"all runs finished: {finished}")
    print(f"turn3 finish_reason: {r3['choices'][0]['finish_reason']} (need stop)")
    print("PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
