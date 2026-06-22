#!/usr/bin/env python3
"""Tests for agent_exec recovery paths."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

from reference.agent_exec import (  # noqa: E402
    detect_agent_phase,
    detect_markdown_shell,
    exclude_stale_refs,
    extract_bash_commands,
    is_allowlisted_command,
    parse_function_xml,
    postprocess_agent_response,
    sanitize_agent_response,
    synthetic_tool_response,
)


def test_exclude_stale_refs():
    pack, changed = exclude_stale_refs("shell_task", "벤치마킹 해봐", ["current_query", "tool_result_refs", "rules"])
    assert "tool_result_refs" not in pack
    assert changed
    print("exclude_stale_refs: OK")


def test_markdown_to_synthetic():
    resp = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "run this:\n```bash\npwd\n```"},
                "finish_reason": "stop",
            }
        ]
    }
    assert detect_markdown_shell(resp["choices"][0]["message"]["content"])
    cmd = extract_bash_commands(resp["choices"][0]["message"]["content"])[0]
    assert cmd == "pwd"
    assert is_allowlisted_command(cmd)
    out = synthetic_tool_response(resp, cmd)
    assert out["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "Shell"
    print("markdown_to_synthetic: OK")


def test_postprocess_with_retry_fail():
    resp = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "```bash\npwd\n```"},
                "finish_reason": "stop",
            }
        ]
    }

    def retry_call(_body):
        return resp

    out, log = postprocess_agent_response(resp, "shell_task", "명령 확인", retry_call=retry_call, retry_body={})
    assert log.synthetic_tool_call or log.fallback
    print("postprocess: OK", log)


def test_strip_tool_content():
    resp = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "I'll help you check that.",
                "tool_calls": [{"id": "x", "type": "function", "function": {"name": "Shell", "arguments": "{}"}}],
            },
            "finish_reason": "stop",
        }]
    }
    out, log = sanitize_agent_response(resp, phase="tool_planning")
    assert out["choices"][0]["message"]["content"] == ""
    assert out["choices"][0]["message"]["tool_calls"]
    assert log.content_stripped
    print("strip_tool_content: OK")


def test_no_null_in_tool_planning_sse():
    from reference.agent_exec import completion_json_to_sse

    resp = {
        "id": "x",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}],
            },
            "finish_reason": "tool_calls",
        }]
    }
    sse = completion_json_to_sse(resp, phase="tool_planning").decode("utf-8")
    assert '"content": null' not in sse
    assert '"content": "null"' not in sse
    assert "Read" in sse
    print("no_null_in_tool_planning_sse: OK")


def test_reasoning_not_in_tool_planning_sse():
    from reference.agent_exec import completion_json_to_sse, normalize_client_response

    reasoning = "요청: 프로젝트 구조 파악\n단계: tool_planning"
    resp = {
        "id": "x",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "reasoning_content": reasoning,
                "tool_calls": [{"id": "1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}],
            },
            "finish_reason": "tool_calls",
        }]
    }
    out = normalize_client_response(resp, phase="tool_planning")
    assert out["choices"][0]["message"].get("reasoning_content") == reasoning
    sse = completion_json_to_sse(out, phase="tool_planning").decode("utf-8")
    assert "reasoning_content" not in sse
    assert "tool_calls" in sse
    print("reasoning_not_in_tool_planning_sse: OK")


def test_build_cursor_reasoning():
    from cursor_reasoning import build_cursor_reasoning

    text = build_cursor_reasoning(
        agent_plan={
            "goal": "구조 파악",
            "task_intent": "general",
            "next_action": {"tool": "Read", "target": "/tmp/README.md", "reason": "루트 확인"},
            "avoid_actions": ["Glob unless known file Read fails"],
        },
        phase="tool_planning",
    )
    assert "구조 파악" in text
    assert "Read" in text
    assert "회피" in text
    print("build_cursor_reasoning: OK")


def test_detect_agent_phase_min_tool_calls():
    """phase stays tool_planning until MIN_TOOL_CALLS_FOR_FINAL_ANSWER distinct calls."""
    import os
    os.environ["MIN_TOOL_CALLS_FOR_FINAL_ANSWER"] = "3"

    def make_body(tool_call_turns: int) -> dict:
        msgs = [{"role": "user", "content": "진단해줘"}]
        for _ in range(tool_call_turns):
            msgs.append({"role": "assistant", "content": "",
                          "tool_calls": [{"id": "x", "type": "function",
                                          "function": {"name": "Read", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "content": "some file content that is long enough"})
        return {"messages": msgs}

    # 1 tool call turn → still tool_planning
    assert detect_agent_phase(make_body(1), "code_edit", True) == "tool_planning", "1 turn should be tool_planning"
    # 2 tool call turns → still tool_planning
    assert detect_agent_phase(make_body(2), "code_edit", True) == "tool_planning", "2 turns should be tool_planning"
    # 3 tool call turns → final_answer
    assert detect_agent_phase(make_body(3), "code_edit", True) == "final_answer", "3 turns should be final_answer"
    print("detect_agent_phase_min_tool_calls: OK")


def test_xml_read_to_synthetic():
    content = "<function=Read>\n<parameter=path>\nrouter/main.py"
    parsed = parse_function_xml(content)
    assert parsed == ("Read", {"path": "router/main.py"})
    resp = {
        "choices": [{
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }]
    }
    out, log = postprocess_agent_response(resp, "code_edit", "read main.py")
    assert log.synthetic_tool_call
    tc = out["choices"][0]["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "Read"
    assert json.loads(tc["function"]["arguments"]) == {"path": "router/main.py"}
    print("xml_read_to_synthetic: OK")


def test_xml_grep_to_synthetic():
    content = "<function=Grep>\n<parameter=pattern>\nroute_backend\n<parameter=path>\nintent_router.py"
    parsed = parse_function_xml(content)
    assert parsed is not None
    assert parsed[0] == "Grep"
    assert parsed[1]["pattern"] == "route_backend"
    print("xml_grep_to_synthetic: OK")


def test_sanitize_pack_labels():
    resp = {
        "choices": [{
            "message": {"role": "assistant", "content": "[Task]\nfoo\nI'll help you\n\n실제 답변입니다."},
            "finish_reason": "stop",
        }]
    }
    out, log = sanitize_agent_response(resp, phase="final_answer")
    content = out["choices"][0]["message"]["content"]
    assert "[Task]" not in content
    assert "I'll help you" not in content
    assert "실제 답변" in content
    print("sanitize_pack_labels: OK")


if __name__ == "__main__":
    test_exclude_stale_refs()
    test_markdown_to_synthetic()
    test_postprocess_with_retry_fail()
    test_strip_tool_content()
    test_no_null_in_tool_planning_sse()
    test_reasoning_not_in_tool_planning_sse()
    test_build_cursor_reasoning()
    test_detect_agent_phase_min_tool_calls()
    test_xml_read_to_synthetic()
    test_xml_grep_to_synthetic()
    test_sanitize_pack_labels()
    print("all passed")
