#!/usr/bin/env python3
"""Validate failed tool compaction and prompt_builder canonical tails."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

from failed_action import (  # noqa: E402
    detect_tool_failure,
    failure_key,
    format_failed_tools_for_planner,
    record_failed_tool,
)
from adapters.memory import (  # noqa: E402
    Artifact,
    DeltaMessage,
    RequestDelta,
    SessionState,
    _save_artifact,
)
from prompt_builder import (  # noqa: E402
    _artifact_session_tail,
    _build_tool_context,
    _tool_planning_tail,
    build_memory_proxy_body,
)


def test_detect_large_file_failure():
    text = "File content (120000 characters) exceeds maximum allowed characters (100000)"
    f = detect_tool_failure("Read", "/tmp/benchmark-score-100.log", text)
    assert f is not None
    assert f["reason"] == "large_file"
    assert "grep" in f["next_allowed"]
    print("detect_large_file_failure: OK")


def test_record_failed_tool_key_and_count():
    state = SessionState()
    failure = detect_tool_failure(
        "Read",
        "tmp/benchmark-runtime-score.json",
        "File content (120000 characters) exceeds maximum allowed characters (100000)",
    )
    assert failure is not None
    record_failed_tool(state, failure)
    record_failed_tool(state, failure)
    assert len(state.failed_tool_summaries) == 1
    item = state.failed_tool_summaries[0]
    assert item["count"] == 2
    assert item.get("last_seen_at")
    key = failure_key(item["tool"], item["path"], item["reason"])
    assert state.failed_actions[key] == 2
    block = format_failed_tools_for_planner(state)
    assert "Recent blocked/failed actions:" in block
    assert "Do not full-read" in block
    assert "x2" not in block  # compact planner lines omit raw counts
    print("record_failed_tool_key_and_count: OK")


def test_failure_skips_artifact():
    state = SessionState()
    delta = RequestDelta(
        delta_id="d",
        req_id="r",
        prev_req_id=None,
        prev_message_count=0,
        curr_message_count=1,
        added_count=1,
        added=[],
        has_new_user=False,
        last_role="tool",
        diff_mode="append_only",
    )
    dm = DeltaMessage(
        index=0,
        role="tool",
        chars=200,
        fingerprint="x",
        preview="too large",
        tool_name="Read",
    )
    msg = {
        "role": "tool",
        "tool_call_id": "tc1",
        "name": "Read",
        "content": "File content (120000 characters) exceeds maximum allowed characters (100000)",
    }
    art = _save_artifact("r", delta, msg, dm, [msg], state=state)
    assert art is None
    assert len(state.failed_tool_summaries) == 1
    print("failure_skips_artifact: OK")


def test_artifact_tail_no_body_scan():
    arts = [
        Artifact(
            artifact_id="a1",
            req_id="r1",
            delta_id="d1",
            type="file_read",
            name="Read",
            path="docs/BENCHMARK.md",
            raw_path="",
            chars=100,
            summary="benchmark summary line",
        ),
    ]
    tail = _artifact_session_tail(arts, budget_tokens=400)
    assert len(tail) == 1
    assert tail[0]["role"] == "tool"
    print("artifact_tail_no_body_scan: OK")


def test_tool_context_from_delta_not_last_role():
    body = {
        "messages": [
            {"role": "user", "content": "reminder", "name": "system"},
            {
                "role": "tool",
                "tool_call_id": "tc9",
                "name": "Grep",
                "content": "grep hit line",
            },
        ]
    }
    delta = RequestDelta(
        delta_id="d",
        req_id="r",
        prev_req_id=None,
        prev_message_count=1,
        curr_message_count=2,
        added_count=2,
        added=[
            DeltaMessage(0, "user", 10, "u", "reminder"),
            DeltaMessage(1, "tool", 20, "t", "grep hit", tool_name="Grep"),
        ],
        has_new_user=False,
        last_role="user",  # not tool — must still find tool_result in delta
        diff_mode="append_only",
    )
    msgs, source = _build_tool_context(body, 400, delta=delta, artifacts=[])
    assert len(msgs) == 1
    assert "grep" in msgs[0]["content"].lower()
    assert source == "delta"
    tail = _tool_planning_tail(body, 400, delta=delta, artifacts=[])
    assert len(tail) == 1
    print("tool_context_from_delta_not_last_role: OK")


def test_memory_proxy_uses_state_query():
    state = SessionState(current_query="analyze benchmark score")
    delta = RequestDelta(
        delta_id="d",
        req_id="r",
        prev_req_id=None,
        prev_message_count=0,
        curr_message_count=1,
        added_count=1,
        added=[],
        has_new_user=True,
        last_role="user",
        diff_mode="append_only",
    )
    body = {"messages": [{"role": "user", "content": "ignored old"}], "max_tokens": 2048}
    from context_cache import ContextIndex

    idx = ContextIndex(
        req_id="r",
        query="analyze benchmark score",
        raw_tokens=100,
        message_count=1,
        tool_count=0,
    )
    out, phase = build_memory_proxy_body(
        body, state, delta, [], "explain", "final_answer", "long", idx, query=""
    )
    assert state.last_prompt_sources.get("query") == "canonical"
    user_msgs = [m for m in out["messages"] if m.get("role") == "user"]
    assert user_msgs
    assert "benchmark score" in user_msgs[0]["content"]
    print("memory_proxy_uses_state_query: OK", phase)


def main():
    test_detect_large_file_failure()
    test_record_failed_tool_key_and_count()
    test_failure_skips_artifact()
    test_artifact_tail_no_body_scan()
    test_tool_context_from_delta_not_last_role()
    test_memory_proxy_uses_state_query()
    print("ALL OK")


if __name__ == "__main__":
    main()
