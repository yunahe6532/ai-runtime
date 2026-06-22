#!/usr/bin/env python3
"""Validate 2-pass intent router classification."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

from context_cache import build_context_index  # noqa: E402
from intent_router import AGENT_FAST_FORBIDDEN, classify_intent, process_two_pass, route_backend  # noqa: E402
from intent_router import IntentResult  # noqa: E402


def _body(query: str, with_tools: bool = True) -> dict:
    body = {
        "model": "model.gguf",
        "stream": True,
        "messages": [
            {"role": "system", "content": "You are a coding assistant." * 100},
            {"role": "user", "content": f"<user_query>\n{query}\n</user_query>"},
        ],
    }
    if with_tools:
        body["tools"] = [{"type": "function", "function": {"name": "Shell"}}]
    return body


def test_casual():
    b = _body("3.11이랑 3.9중 뭐가 커?", with_tools=True)
    idx = build_context_index(b, "test1")
    intent = classify_intent(idx.query, idx)
    assert intent.intent == "casual"
    assert intent.route == "fast"
    _, backend, stats, _, _ = process_two_pass(b)
    assert backend == "fast"
    assert stats.tools_stripped is True
    print("casual: OK")


def test_benchmark():
    q = "스크립트 짜서 서버로그 확인하고 벤치마킹 해봐"
    b = _body(q)
    idx = build_context_index(b, "test2")
    intent = classify_intent(idx.query, idx)
    assert intent.intent in ("benchmark", "shell_task")
    assert intent.route == "main"
    assert intent.needs_tools is True
    _, backend, stats, intent2, _ = process_two_pass(b)
    assert backend == "long", f"benchmark must not use fast, got {backend}"
    assert stats.tools_stripped is False
    proxy, _, _, _, _ = process_two_pass({**b, "stream": True})
    assert proxy.get("stream") is False
    print("benchmark: OK", intent.intent, "backend=", backend)


def test_code_edit_backend():
    q = "docker-compose.yml, router/main.py 파일 읽고 확인해줘"
    b = _body(q)
    idx = build_context_index(b, "test_code_edit")
    intent = classify_intent(idx.query, idx)
    assert intent.intent == "code_edit"
    assert intent.route == "main"
    backend, reason = route_backend(intent, pack_tokens=500, sticky_long=False)
    assert backend == "long"
    assert reason == "agent_task_fast_forbidden"
    _, backend2, _, _, _ = process_two_pass(b)
    assert backend2 == "long"
    print("code_edit_backend: OK", backend2)


def test_agent_fast_forbidden():
    for intent_name in sorted(AGENT_FAST_FORBIDDEN):
        intent = IntentResult(
            intent=intent_name,
            route="main",
            needs_tools=True,
            needs_files=False,
            needs_shell=False,
            needs_prior_summary=False,
            needs_raw_tool_results=False,
            needs_full_raw_context=False,
            context_budget_tokens=12000,
            context_pack=["current_query"],
        )
        backend, reason = route_backend(intent, pack_tokens=500, sticky_long=False)
        assert backend != "fast", f"{intent_name} must not route to fast"
        assert reason == "agent_task_fast_forbidden"
    print("agent_fast_forbidden: OK")


def test_debug_no_prior():
    q = "같은 질문하니까 이전 답을 다시 뱉는데?"
    b = _body(q)
    idx = build_context_index(b, "test3")
    intent = classify_intent(idx.query, idx)
    assert intent.intent == "debug"
    _, _, stats, _, _ = process_two_pass(b)
    pack = process_two_pass(b)[0]["messages"][1]["content"]
    assert "[recent assistant]" not in pack
    print("debug: OK")


def test_mixed_query_prefers_analysis():
    q = "라우터 로그 기반으로 개선점 찾고 docker-compose.yml 파일 읽고 확인해줘"
    b = _body(q)
    idx = build_context_index(b, "test_mixed")
    intent = classify_intent(idx.query, idx)
    assert intent.intent in ("log_analysis", "explain", "code_edit"), f"got {intent.intent}"
    print("mixed_query_analysis: OK", intent.intent, intent.reason)


def test_log_analysis_query():
    q = "로그 분석 및 문제점 어떤게 있는지 상세분석해봐"
    b = _body(q)
    idx = build_context_index(b, "test_log_analysis")
    intent = classify_intent(idx.query, idx)
    assert intent.intent in ("log_analysis", "explain", "project_inspection"), f"got {intent.intent}"
    assert intent.intent != "code_edit"
    print("log_analysis_query: OK", intent.intent, intent.reason)


def test_multifile_query_forces_code_edit():
    q = "docker-compose.yml, router/main.py, router/agent_exec.py를 읽고 확인해줘"
    b = _body(q)
    idx = build_context_index(b, "test_multifile")
    intent = classify_intent(idx.query, idx)
    assert intent.intent == "code_edit", f"expected code_edit, got {intent.intent} ({intent.reason})"
    assert intent.needs_shell is True, "code_edit should have needs_shell=True"
    print("multifile_query_code_edit: OK", intent.reason)


def test_curl_test_stays_code_edit():
    q = "docker-compose.yml, router/main.py, router/agent_exec.py를 읽고 curl 테스트까지 실행해서 결과 정리해줘"
    b = _body(q)
    idx = build_context_index(b, "test_curl_code_edit")
    intent = classify_intent(idx.query, idx)
    assert intent.intent == "code_edit", f"expected code_edit, got {intent.intent} ({intent.reason})"
    assert intent.needs_shell is True, "must have needs_shell=True for curl test"
    print("curl_test_stays_code_edit: OK", intent.reason)


def test_exec_context_keeps_original_system():
    q = "docker-compose.yml 파일 읽고 확인해줘"
    b = _body(q)
    proxy, _, _, intent, phase = process_two_pass(b)
    assert intent.intent == "code_edit"
    assert phase in ("", "tool_planning")
    assert proxy["messages"][0]["role"] == "system"
    sys_content = proxy["messages"][0]["content"]
    assert "coding assistant" in sys_content or "TOOL PLANNING" in sys_content
    print("exec_context_original_system: OK")


def test_intent_tokens_small():
    b = _body("3.11이랑 3.9중 뭐가 커?")
    _, _, stats, _, _ = process_two_pass(b)
    assert stats.intent_tokens < 2000, stats.intent_tokens
    print("intent_tokens_small: OK", stats.intent_tokens)


if __name__ == "__main__":
    test_casual()
    test_benchmark()
    test_code_edit_backend()
    test_agent_fast_forbidden()
    test_debug_no_prior()
    test_mixed_query_prefers_analysis()
    test_log_analysis_query()
    test_multifile_query_forces_code_edit()
    test_curl_test_stays_code_edit()
    test_exec_context_keeps_original_system()
    test_intent_tokens_small()
    print("all passed")
