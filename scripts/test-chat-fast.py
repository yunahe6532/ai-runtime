#!/usr/bin/env python3
"""Quick checks for chat_fast path."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

from chat_fast import build_simple_chat_body, is_simple_qa  # noqa: E402


def test_simple():
    msgs = [{"role": "user", "content": "strawberry에 r이 몇 개있지?"}]
    assert is_simple_qa(msgs)
    body = build_simple_chat_body({"model": "m", "messages": msgs, "tools": [{}], "stream": True})
    assert "tools" not in body
    assert len(body["messages"]) == 2
    assert body["messages"][1]["content"] == "strawberry에 r이 몇 개있지?"
    print("simple_qa: OK")


def test_cursor_wrapped():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "<open_and_recently_viewed_files>...</open_and_recently_viewed_files>"},
                {"type": "text", "text": "<user_query>\nstrawberry에 r 몇 개?\n</user_query>"},
            ],
        }
    ]
    assert is_simple_qa(msgs)
    body = build_simple_chat_body({"model": "m", "messages": msgs})
    assert body["messages"][1]["content"] == "strawberry에 r 몇 개?"
    print("cursor_wrapped_simple: OK")


def test_coding_not_simple():
    msgs = [{"role": "user", "content": "router 코드 수정해줘"}]
    assert not is_simple_qa(msgs)
    print("coding_not_simple: OK")


def test_work_request_not_simple():
    msgs = [
        {
            "role": "user",
            "content": "<user_query>\n스크립트 짜서 서버로그 확인하고 벤치마킹 해봐\n</user_query>",
        }
    ]
    assert not is_simple_qa(msgs)
    print("work_request_not_simple: OK")


if __name__ == "__main__":
    test_simple()
    test_cursor_wrapped()
    test_coding_not_simple()
    test_work_request_not_simple()
    print("all passed")
