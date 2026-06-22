#!/usr/bin/env python3
"""Regression gate: agent loop must never emit empty client responses."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from reference.agent_exec import postprocess_agent_response  # noqa: E402
from reference.plan_state import resolve_agent_phase  # noqa: E402
from reference.response_guard import (  # noqa: E402
    apply_nonempty_guard,
    is_empty_outgoing,
    parse_qwen_tool_start_xml,
    parse_tool_call_content,
)
from intent_router import classify_intent, ContextIndex  # noqa: E402


def _msg(content: str = "", tool_calls: list | None = None) -> dict:
    m: dict = {"role": "assistant", "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": m, "finish_reason": "stop"}]}


def _assert_not_empty(label: str, resp: dict) -> None:
    if is_empty_outgoing(resp):
        raise AssertionError(f"{label}: empty outgoing (tool_calls=0 and content=0)")


def test_no_empty_response_on_bad_ping_pong() -> None:
    xml = (
        "<tool_call>\n"
        "<|tool_start|>Read<|tool_sep|>path=/home/yunahe/ai-runtime/cursor-local-llm/router/runtime_core/__init__.py\n"
        "<tool_call><|tool_start|>Read<|tool_sep|>path=/home/yunahe/ai-runtime/cursor-local-llm/router/adapters/__init__.py"
    )
    resp = _msg(xml)
    out, log = postprocess_agent_response(
        resp,
        "code_edit",
        "프로젝트 구조 분석",
        phase="tool_planning",
    )
    _assert_not_empty("bad_ping_pong_xml", out)
    assert log.fallback or parse_tool_call_content(xml)
    print("test_no_empty_response_on_bad_ping_pong: OK")


def test_next_action_answer_requires_coverage() -> None:
    from adapters.memory import SessionState

    q = (
        "이 프로젝트 구조를 분석해서 runtime_core, adapters, legacy, integrations 역할을 요약해줘. "
        "코드는 수정하지 말고 필요한 파일만 읽어서 근거와 함께 답해."
    )
    state = SessionState()
    state.agent_plan = {
        "task_intent": "project_inspection",
        "router_intent": "read_only_analysis",
        "next_action": {"tool": "answer", "target": "", "reason": "done"},
        "evidence_needed": ["target_coverage"],
        "evidence_collected": ["project_tree_seen:router"],
        "coverage_hits": [],
        "preferred_sources": ["docs/MODULE_MAP.md", "router/runtime_core"],
        "final_ready": True,
    }
    state.last_ingest_metrics = {"diff_mode": "append_only", "messages_new": 1}

    body = {
        "messages": [
            {"role": "user", "content": q},
            {"role": "tool", "name": "Shell", "content": "ok"},
        ]
    }
    phase = resolve_agent_phase(body, state, q, "read_only_analysis", True)
    assert phase != "final_answer", phase
    print("test_next_action_answer_requires_coverage: OK")


def test_read_only_analysis_not_code_edit() -> None:
    q = (
        "이 프로젝트 구조를 분석해서 runtime_core, adapters, legacy, integrations 역할을 요약해줘. "
        "코드는 수정하지 말고 필요한 파일만 읽어서 근거와 함께 답해."
    )
    index = ContextIndex(
        req_id="test",
        query=q,
        raw_tokens=100,
        message_count=1,
        tool_count=0,
    )
    intent = classify_intent(q, index)
    assert intent.intent == "read_only_analysis", intent.intent
    assert intent.intent != "code_edit"
    print("test_read_only_analysis_not_code_edit: OK")


def test_qwen_tool_start_xml_parsed() -> None:
    content = (
        "<tool_call>\n"
        "<|tool_start|>Read<|tool_sep|>path=/tmp/foo.py\n"
    )
    parsed = parse_qwen_tool_start_xml(content)
    assert parsed is not None, "parse_qwen_tool_start_xml failed"
    tool, args = parsed
    assert tool == "Read"
    assert args.get("path") == "/tmp/foo.py"
    print("test_qwen_tool_start_xml_parsed: OK")


def test_xml_parse_failure_not_empty() -> None:
    resp = _msg("<tool_call>broken-no-tool-start")
    out, _ = apply_nonempty_guard(
        resp,
        phase="tool_planning",
        intent_name="code_edit",
        query="분석",
        reason="xml_parse_failure",
    )
    _assert_not_empty("xml_parse_failure", out)
    print("test_xml_parse_failure_not_empty: OK")


def main() -> int:
    tests = [
        test_no_empty_response_on_bad_ping_pong,
        test_next_action_answer_requires_coverage,
        test_read_only_analysis_not_code_edit,
        test_qwen_tool_start_xml_parsed,
        test_xml_parse_failure_not_empty,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}", file=sys.stderr)
    print(json.dumps({"passed": len(tests) - failed, "failed": failed, "total": len(tests)}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
