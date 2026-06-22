#!/usr/bin/env python3
"""Cursor-style agent benchmark: tool_call quality + latency before/after model swap."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tmp" / "benchmark-cursor-agent.json"
ROUTER = "http://localhost:8080"
MODEL = "model.gguf"

# 1x1 PNG (red pixel) — vision smoke test
TINY_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

CURSOR_SYSTEM = (
    "You are an AI coding assistant, powered by model.gguf.\n"
    "You operate in Cursor.\n"
    "You are a coding agent in the Cursor IDE that helps the USER with software engineering tasks.\n"
    "Your main goal is to follow the USER's instructions, which are denoted by the <user_query> tag.\n"
    "Only emit tool_calls in tool planning phase. No filler prose."
)

TOOLS_SHELL = [
    {
        "type": "function",
        "function": {
            "name": "Shell",
            "description": "Execute shell command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}, "description": {"type": "string"}},
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
            "name": "Glob",
            "description": "Find files",
            "parameters": {
                "type": "object",
                "properties": {
                    "glob_pattern": {"type": "string"},
                    "target_directory": {"type": "string"},
                },
                "required": ["glob_pattern"],
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
                "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
]


@dataclass
class CaseResult:
    name: str
    ok: bool
    wall_ms: float
    prompt_tokens: int
    completion_tokens: int
    gen_tps: float
    has_tool_calls: bool
    tool_names: list[str]
    expected_tools: list[str]
    tool_match: bool
    json_valid: bool | None  # None if no tool_calls
    has_xml_leak: bool
    final_tool_leak: bool
    has_filler: bool
    finish_reason: str
    content_preview: str
    case_type: str  # tool | explain | vision | final_answer
    vision_ok: bool = False
    error: str = ""


@dataclass
class BenchReport:
    profile: str
    model_label: str
    router_status: dict[str, Any]
    cases: list[CaseResult] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def router_status() -> dict[str, Any]:
    try:
        return httpx.get(f"{ROUTER}/router/status", timeout=10.0).json()
    except Exception as exc:
        return {"error": str(exc)}


def active_profile() -> tuple[str, str]:
    pf = ROOT / "configs" / "model-profiles.env"
    env = ROOT / ".env"
    profile = "unknown"
    label = ""
    if pf.exists():
        lines = pf.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if line.startswith("ACTIVE_PROFILE="):
                profile = line.split("=", 1)[1].strip()
        for line in lines:
            m = re.match(rf"^PROFILE_{re.escape(profile)}_LABEL=(.*)$", line)
            if m:
                label = m.group(1)
                break
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ACTIVE_PROFILE="):
                profile = line.split("=", 1)[1].strip()
    return profile, label


def user_msg(query: str, workspace: str = "/home/yunahe/ai-runtime/cursor-local-llm") -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            f"<user_info>\nWorkspace Path: {workspace}\n</user_info>\n\n"
            f"<open_and_recently_viewed_files>\n- {workspace}/docker-compose.yml\n"
            f"</open_and_recently_viewed_files>\n\n"
            f"<user_query>\n{query}\n</user_query>"
        ),
    }


def build_cases(include_vision: bool = False) -> list[tuple[str, dict[str, Any], list[str], str]]:
    ws = "/home/yunahe/ai-runtime/cursor-local-llm"
    cases: list[tuple[str, dict[str, Any], list[str], str]] = [
        (
            "agent_shell_docker_ps",
            {
                "model": MODEL,
                "stream": False,
                "max_tokens": 400,
                "tools": TOOLS_SHELL,
                "messages": [
                    {"role": "system", "content": CURSOR_SYSTEM},
                    user_msg("docker ps로 router 컨테이너 상태 확인해줘. Shell 반드시 사용."),
                ],
            },
            ["Shell"],
            "tool",
        ),
        (
            "agent_read_compose",
            {
                "model": MODEL,
                "stream": False,
                "max_tokens": 400,
                "tools": TOOLS_SHELL,
                "messages": [
                    {"role": "system", "content": CURSOR_SYSTEM},
                    user_msg(f"{ws}/docker-compose.yml 의 llama-long 서비스 설정만 Read로 확인해줘."),
                ],
            },
            ["Read"],
            "tool",
        ),
        (
            "agent_glob_router",
            {
                "model": MODEL,
                "stream": False,
                "max_tokens": 400,
                "tools": TOOLS_SHELL,
                "messages": [
                    {"role": "system", "content": CURSOR_SYSTEM},
                    user_msg(f"{ws}/router 디렉토리의 *.py 파일 목록 Glob으로 찾아줘."),
                ],
            },
            ["Glob"],
            "tool",
        ),
        (
            "agent_grep_route",
            {
                "model": MODEL,
                "stream": False,
                "max_tokens": 400,
                "tools": TOOLS_SHELL,
                "messages": [
                    {"role": "system", "content": CURSOR_SYSTEM},
                    user_msg("route_backend 함수가 어디 있는지 Grep으로 찾아줘."),
                ],
            },
            ["Grep"],
            "tool",
        ),
        (
            "explain_short",
            {
                "model": MODEL,
                "stream": False,
                "max_tokens": 200,
                "messages": [
                    {"role": "system", "content": CURSOR_SYSTEM},
                    user_msg("TOKEN_THRESHOLD 환경변수가 뭐하는 건지 한 줄로 설명해줘."),
                ],
            },
            [],
            "explain",
        ),
        (
            "final_answer_no_tools",
            {
                "model": MODEL,
                "stream": False,
                "max_tokens": 300,
                "messages": [
                    {"role": "system", "content": CURSOR_SYSTEM + "\nFINAL ANSWER: prose only, no tools."},
                    user_msg("docker-compose.yml에 router 서비스 포트가 뭐야? 한 줄로."),
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_bench_1",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": json.dumps({"path": f"{ws}/docker-compose.yml"}),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_bench_1",
                        "content": 'ports:\n  - "${PORT:-8080}:8080"',
                    },
                ],
            },
            [],
            "final_answer",
        ),
    ]
    if include_vision:
        cases.append(
            (
                "vision_tiny_png",
                {
                    "model": MODEL,
                    "stream": False,
                    "max_tokens": 256,
                    "messages": [
                        {"role": "system", "content": CURSOR_SYSTEM},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": TINY_PNG_DATA_URL},
                                },
                                {
                                    "type": "text",
                                    "text": (
                                        "<user_query>\n"
                                        "이 이미지가 단색인지 한 줄로 설명해줘. "
                                        "색상만 말해.\n</user_query>"
                                    ),
                                },
                            ],
                        },
                    ],
                },
                [],
                "vision",
            )
        )
    return cases


def parse_tool_names(msg: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict):
            fn = tc.get("function", {})
            if isinstance(fn, dict) and fn.get("name"):
                names.append(str(fn["name"]))
    content = str(msg.get("content") or "")
    if "<function=" in content:
        for m in re.finditer(r"<function=(\w+)>", content, re.I):
            names.append(m.group(1))
    return names


def validate_tool_calls_json(msg: dict[str, Any]) -> bool | None:
    tcs = msg.get("tool_calls") or []
    if not tcs:
        return None
    for tc in tcs:
        if not isinstance(tc, dict):
            return False
        fn = tc.get("function", {})
        if not isinstance(fn, dict) or not fn.get("name"):
            return False
        args = fn.get("arguments", "")
        if isinstance(args, dict):
            continue
        try:
            json.loads(str(args))
        except json.JSONDecodeError:
            return False
    return True


def run_case(
    name: str,
    payload: dict[str, Any],
    expected: list[str],
    case_type: str = "tool",
) -> CaseResult:
    t0 = time.perf_counter()
    try:
        r = httpx.post(f"{ROUTER}/v1/chat/completions", json=payload, timeout=180.0)
        wall_ms = (time.perf_counter() - t0) * 1000
        r.raise_for_status()
        data = r.json()
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        usage = data.get("usage", {})
        content = str(msg.get("content") or "")
        tool_names = parse_tool_names(msg)
        has_tc = bool(tool_names) or bool(msg.get("tool_calls"))
        json_valid = validate_tool_calls_json(msg)
        tool_match = not expected or any(t in tool_names for t in expected)
        filler = bool(re.search(r"I'll help|Let me |도와드리", content, re.I))
        xml_leak = "<function=" in content and has_tc
        final_tool_leak = case_type == "final_answer" and (
            bool(msg.get("tool_calls")) or "<function=" in content
        )
        pt = int(usage.get("prompt_tokens", 0))
        ct = int(usage.get("completion_tokens", 0))
        tps = ct / (wall_ms / 1000) if wall_ms > 0 and ct else 0.0

        if case_type == "vision":
            ok = bool(content.strip()) and len(content.strip()) > 3
            vision_ok = ok
        elif case_type == "final_answer":
            ok = bool(content.strip()) and not final_tool_leak
            vision_ok = False
        elif expected:
            ok = tool_match and (json_valid is not False)
            vision_ok = False
        else:
            ok = bool(content.strip())
            vision_ok = False

        return CaseResult(
            name=name,
            ok=ok,
            wall_ms=wall_ms,
            prompt_tokens=pt,
            completion_tokens=ct,
            gen_tps=tps,
            has_tool_calls=has_tc,
            tool_names=tool_names,
            expected_tools=expected,
            tool_match=tool_match,
            json_valid=json_valid,
            has_xml_leak=xml_leak,
            final_tool_leak=final_tool_leak,
            has_filler=filler,
            finish_reason=str(choice.get("finish_reason", "")),
            content_preview=content[:120].replace("\n", " "),
            case_type=case_type,
            vision_ok=vision_ok,
        )
    except Exception as exc:
        return CaseResult(
            name=name,
            ok=False,
            wall_ms=(time.perf_counter() - t0) * 1000,
            prompt_tokens=0,
            completion_tokens=0,
            gen_tps=0.0,
            has_tool_calls=False,
            tool_names=[],
            expected_tools=expected,
            tool_match=False,
            json_valid=None,
            has_xml_leak=False,
            final_tool_leak=False,
            has_filler=False,
            finish_reason="",
            content_preview="",
            case_type=case_type,
            error=str(exc),
        )


def summarize(cases: list[CaseResult]) -> dict[str, Any]:
    ok = [c for c in cases if c.ok]
    tool_cases = [c for c in cases if c.expected_tools]
    json_cases = [c for c in cases if c.json_valid is not None]
    vision_cases = [c for c in cases if c.case_type == "vision"]
    final_cases = [c for c in cases if c.case_type == "final_answer"]
    return {
        "total": len(cases),
        "passed": len(ok),
        "pass_rate": round(100 * len(ok) / len(cases), 1) if cases else 0,
        "tool_cases": len(tool_cases),
        "tool_match_rate": round(
            100 * sum(1 for c in tool_cases if c.tool_match) / len(tool_cases), 1
        )
        if tool_cases
        else 0,
        "json_valid_rate": round(
            100 * sum(1 for c in json_cases if c.json_valid) / len(json_cases), 1
        )
        if json_cases
        else None,
        "vision_pass_rate": round(
            100 * sum(1 for c in vision_cases if c.vision_ok) / len(vision_cases), 1
        )
        if vision_cases
        else None,
        "final_answer_tool_leaks": sum(1 for c in final_cases if c.final_tool_leak),
        "avg_wall_ms": round(sum(c.wall_ms for c in cases) / len(cases), 1) if cases else 0,
        "avg_decode_tps": round(sum(c.gen_tps for c in cases) / len(cases), 2) if cases else 0,
        "avg_gen_tps": round(sum(c.gen_tps for c in cases) / len(cases), 2) if cases else 0,
        "avg_ctx_used_tokens": round(
            sum(c.prompt_tokens + c.completion_tokens for c in cases) / len(cases), 0
        )
        if cases
        else 0,
        "ctx_used_tokens_max": max(
            (c.prompt_tokens + c.completion_tokens for c in cases), default=0
        ),
        "xml_leaks": sum(1 for c in cases if c.has_xml_leak),
        "filler_responses": sum(1 for c in cases if c.has_filler),
        "fallback_triggered": 0,
    }


def llama_timings(container: str = "cursor-local-llm-long") -> dict[str, Any]:
    try:
        r = subprocess.run(
            ["docker", "logs", container, "--tail", "200"],
            capture_output=True,
            text=True,
            check=False,
        )
        text = (r.stdout or "") + (r.stderr or "")
        rows = []
        for m in re.finditer(
            r"prompt eval time =\s+([\d.]+) ms / \s*(\d+) tokens.*?eval time =\s+([\d.]+) ms / \s*(\d+) tokens",
            text,
            re.S,
        ):
            p_ms, p_n, e_ms, e_n = m.groups()
            rows.append(
                {
                    "prompt_tps": float(p_n) / (float(p_ms) / 1000) if float(p_ms) else 0,
                    "gen_tps": float(e_n) / (float(e_ms) / 1000) if float(e_ms) else 0,
                }
            )
        if not rows:
            return {}
        return {
            "samples": len(rows),
            "avg_prompt_tps": round(sum(x["prompt_tps"] for x in rows) / len(rows), 1),
            "avg_gen_tps": round(sum(x["gen_tps"] for x in rows) / len(rows), 1),
            "avg_prefill_ms": round(
                sum(float(m.group(1)) for m in re.finditer(
                    r"prompt eval time =\s+([\d.]+) ms", text
                )) / len(rows),
                1,
            )
            if rows
            else 0,
        }
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Cursor-format agent benchmark")
    ap.add_argument("--label", default="", help="Run label (e.g. before, after)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--compare", default="", help="Previous JSON to diff against")
    ap.add_argument(
        "--with-vision",
        action="store_true",
        help="Include vision_tiny_png case (requires mmproj on long server)",
    )
    args = ap.parse_args()

    profile, model_label = active_profile()
    if args.label:
        model_label = f"{model_label} [{args.label}]"

    print(f"=== Cursor Agent Benchmark === profile={profile} model={model_label}")
    status = router_status()
    print(json.dumps(status, ensure_ascii=False, indent=2))

    report = BenchReport(profile=profile, model_label=model_label, router_status=status)
    for name, payload, expected, case_type in build_cases(include_vision=args.with_vision):
        res = run_case(name, payload, expected, case_type)
        report.cases.append(res)
        mark = "OK" if res.ok else "FAIL"
        print(
            f"  [{mark}] {name} ({case_type}): {res.wall_ms:.0f}ms tools={res.tool_names} "
            f"json={res.json_valid} ctx={res.prompt_tokens}+{res.completion_tokens} "
            f"tps={res.gen_tps:.1f} err={res.error}"
        )

    report.summary = summarize(report.cases)
    timings = llama_timings()
    report.summary["llama_timings"] = timings
    if timings.get("avg_prefill_ms"):
        report.summary["avg_prefill_ms"] = timings["avg_prefill_ms"]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # merge runs in output file
    existing: dict[str, Any] = {"runs": []}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    run_entry = {
        "label": args.label or profile,
        "profile": profile,
        "model_label": model_label,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": report.summary,
        "cases": [asdict(c) for c in report.cases],
        "router_status": status,
    }
    existing.setdefault("runs", []).append(run_entry)
    out_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Summary ===")
    print(json.dumps(report.summary, ensure_ascii=False, indent=2))
    print(f"\nSaved: {out_path}")

    if args.compare:
        prev = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        prev_run = (prev.get("runs") or [{}])[-1]
        prev_s = prev_run.get("summary", {})
        print("\n=== vs previous ===")
        for k in (
            "pass_rate",
            "tool_match_rate",
            "json_valid_rate",
            "vision_pass_rate",
            "final_answer_tool_leaks",
            "avg_wall_ms",
            "avg_decode_tps",
            "avg_prefill_ms",
            "xml_leaks",
        ):
            print(f"  {k}: {prev_s.get(k)} → {report.summary.get(k)}")

    return 0 if report.summary.get("passed", 0) == report.summary.get("total", 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
