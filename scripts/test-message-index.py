#!/usr/bin/env python3
"""Validate incremental message indexing and append-only diff."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

from context_cache import build_context_index  # noqa: E402
from adapters.memory import SessionState, extract_delta, ingest_request, save_state  # noqa: E402
from message_index import (  # noqa: E402
    classify_message_kind,
    diff_messages,
    stable_message_key,
)


def _load_pair():
    raw_dir = Path(__file__).resolve().parents[1] / "tmp" / "context-cache" / "raw"
    a = raw_dir / "1781763260_0047.json"
    b = raw_dir / "1781763269_0048.json"
    if not a.exists() or not b.exists():
        return None, None
    msgs_a = json.loads(a.read_text())["body"]["messages"]
    msgs_b = json.loads(b.read_text())["body"]["messages"]
    return msgs_a, msgs_b


def test_stable_tool_call_id_key():
    msg = {"role": "tool", "tool_call_id": "abc123", "name": "Read", "content": "x" * 5000}
    k1 = stable_message_key(msg)
    msg2 = dict(msg, content="y" * 9000)
    k2 = stable_message_key(msg2)
    assert k1 == k2 == "tool:abc123"
    print("stable_tool_call_id_key: OK")


def test_append_only_real_capture():
    msgs_a, msgs_b = _load_pair()
    if not msgs_a:
        print("append_only_real_capture: SKIP (no raw captures)")
        return
    keys_a = [stable_message_key(m) for m in msgs_a]
    diff = diff_messages(msgs_b, keys_a)
    assert diff.mode == "append_only", diff.mode
    assert diff.messages_new <= 3, diff.messages_new
    assert len(keys_a) == 116
    print("append_only_real_capture: OK", diff.messages_new, "new")


def test_noise_classifier():
    msg = {
        "role": "user",
        "content": "Your response was cut off because it exceeded the output token limit",
    }
    assert classify_message_kind(msg) == "continuation_notice"
    print("noise_classifier: OK")


def test_incremental_context_index():
    msgs_a, msgs_b = _load_pair()
    if not msgs_a:
        print("incremental_context_index: SKIP")
        return
    state = SessionState()
    body_a = {"messages": msgs_a, "tools": []}
    body_b = {"messages": msgs_b, "tools": []}
    d1, s1, _ = ingest_request("t1", body_a, query="q1")
    build_context_index(body_a, "t1", state=s1, delta=d1)
    save_state(s1, None)
    d2, s2, _ = ingest_request("t2", body_b, query="q2")
    build_context_index(body_b, "t2", state=s2, delta=d2)
    assert d2.diff_mode == "append_only"
    assert s2.last_ingest_metrics.get("context_index_mode") == "incremental"
    assert len(msgs_b) == d2.curr_message_count
    print(
        "incremental_context_index: OK",
        "new=",
        d2.added_count,
        "mode=",
        s2.last_ingest_metrics.get("context_index_mode"),
    )


def test_extract_delta_metrics():
    msgs_a, msgs_b = _load_pair()
    if not msgs_a:
        print("extract_delta_metrics: SKIP")
        return
    state = SessionState()
    extract_delta("r1", {"messages": msgs_a}, state)
    state.last_message_count = len(msgs_a)
    delta = extract_delta("r2", {"messages": msgs_b}, state)
    m = state.last_ingest_metrics
    assert m["messages_total"] == len(msgs_b)
    assert m["messages_new"] == delta.added_count
    assert m["diff_mode"] == "append_only"
    print("extract_delta_metrics: OK", m)


if __name__ == "__main__":
    test_stable_tool_call_id_key()
    test_noise_classifier()
    test_append_only_real_capture()
    test_extract_delta_metrics()
    test_incremental_context_index()
    print("all passed")
