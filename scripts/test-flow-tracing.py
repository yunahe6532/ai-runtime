#!/usr/bin/env python3
"""Test OTel flow tracing facade."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

os.environ["OTEL_FLOW_TRACE"] = "1"
os.environ["FLOW_TRACE"] = "0"

from adapters.trace import begin_flow, record_proxy, record_response  # noqa: E402


def test_flow_facade():
    body = {"messages": [{"role": "user", "content": "test"}], "max_tokens": 100}
    fid = begin_flow(body, flow_id="test_flow_001")
    assert fid == "test_flow_001"
    record_proxy(fid, body, intent="agent", phase="tool_planning", backend="long", raw_tokens=1000, pack_tokens=500, saved_pct=50.0)
    record_response(fid, {"choices": [{"message": {"content": "ok", "tool_calls": []}}]}, elapsed_sec=1.2, phase="tool_planning")
    print("flow_facade: OK")


if __name__ == "__main__":
    test_flow_facade()
    print("ALL OK")
