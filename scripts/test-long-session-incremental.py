#!/usr/bin/env python3
"""Long-session incremental replay — metrics and prompt_source checks."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

_tmp = tempfile.mkdtemp(prefix="ctx-cache-long-")
os.environ["CONTEXT_CACHE_DIR"] = _tmp

from context_cache import build_context_index  # noqa: E402
from failed_action import detect_tool_failure, record_failed_tool  # noqa: E402
from adapters.memory import SessionState, ingest_request, save_state  # noqa: E402
from message_index import diff_messages, stable_message_key  # noqa: E402
from prompt_builder import build_memory_proxy_body  # noqa: E402


def _load_pair():
    raw_dir = ROOT / "tmp" / "context-cache" / "raw"
    a = raw_dir / "1781763260_0047.json"
    b = raw_dir / "1781763269_0048.json"
    if not a.exists() or not b.exists():
        return None, None
    msgs_a = json.loads(a.read_text())["body"]["messages"]
    msgs_b = json.loads(b.read_text())["body"]["messages"]
    return msgs_a, msgs_b


def test_long_session_incremental():
    msgs_a, msgs_b = _load_pair()
    if not msgs_a:
        print("long_session_incremental: SKIP (no raw captures)")
        return

    assert len(msgs_b) >= 100, len(msgs_b)
    keys_a = [stable_message_key(m) for m in msgs_a]
    diff = diff_messages(msgs_b, keys_a)
    assert diff.mode == "append_only"
    assert diff.messages_new <= 3

    state = SessionState()
    d1, s1, arts1 = ingest_request("long_t1", {"messages": msgs_a, "tools": []}, query="bench")
    build_context_index({"messages": msgs_a, "tools": []}, "long_t1", state=s1, delta=d1)
    save_state(s1, None)

    d2, s2, arts2 = ingest_request("long_t2", {"messages": msgs_b, "tools": []}, query="bench")
    build_context_index({"messages": msgs_b, "tools": []}, "long_t2", state=s2, delta=d2)

    m = s2.last_ingest_metrics
    assert m.get("messages_total") == len(msgs_b)
    assert m.get("messages_new") == d2.added_count <= 3
    assert m.get("diff_mode") == "append_only"
    assert m.get("context_index_mode") == "incremental"
    assert m.get("phase_update_mode") == "event"

    fail = detect_tool_failure(
        "Read",
        "tmp/benchmark-runtime-score.json",
        "File content (99999 characters) exceeds maximum allowed characters (100000)",
    )
    assert fail
    s2.failed_tool_summaries = []
    record_failed_tool(s2, fail)
    record_failed_tool(s2, fail)
    assert s2.failed_tool_summaries[0]["count"] == 2

    from context_cache import ContextIndex

    idx = ContextIndex(
        req_id="long_t2",
        query=s2.current_query or "bench",
        raw_tokens=5000,
        message_count=len(msgs_b),
        tool_count=0,
    )
    proxy, phase = build_memory_proxy_body(
        {"messages": msgs_b, "max_tokens": 4096, "tools": []},
        s2,
        d2,
        arts2,
        "agent",
        "tool_planning",
        "long",
        idx,
    )
    sources = s2.last_prompt_sources
    assert sources.get("query") == "canonical"
    assert sources.get("tool_context") in ("delta", "artifact", "none")
    assert sources.get("session_tail") in ("none", "artifact", "canonical")
    assert sources.get("tool_context") != "body_fallback" or not arts2
    assert proxy.get("messages")
    print(
        "long_session_incremental: OK",
        f"total={m.get('messages_total')}",
        f"new={m.get('messages_new')}",
        f"sources={sources}",
        f"failed_count={s2.failed_tool_summaries[0]['count']}",
        f"phase={phase}",
    )


if __name__ == "__main__":
    test_long_session_incremental()
    print("all passed")
