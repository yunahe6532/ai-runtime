#!/usr/bin/env python3
"""Validate large-file Read guard redirects."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

from reference.read_guard import (  # noqa: E402
    build_read_alternatives,
    check_read_allowed,
    is_large_json_log_path,
)


def test_json_log_paths():
    assert is_large_json_log_path("tmp/benchmark-runtime-score.json")
    assert is_large_json_log_path("tmp/cursor-captures/foo.flow.json")
    assert not is_large_json_log_path("router/main.py")
    print("json_log_paths: OK")


def test_block_full_read_json():
    path = "/home/yunahe/ai-runtime/cursor-local-llm/tmp/benchmark-runtime-score.json"
    allowed, reason, info = check_read_allowed(path, {})
    if Path(path).is_file():
        assert not allowed, f"expected block, got allowed reason={reason}"
        assert info and info.get("next_allowed")
        print("block_full_read_json: OK", reason)
    else:
        print("block_full_read_json: SKIP (file missing)")


def test_allow_range_read():
    path = "/home/yunahe/ai-runtime/cursor-local-llm/tmp/benchmark-runtime-score.json"
    allowed, reason, _ = check_read_allowed(path, {"offset": 1, "limit": 80})
    assert allowed, f"range read should be allowed, reason={reason}"
    print("allow_range_read: OK")


def test_alternatives_nonempty():
    info = build_read_alternatives("tmp/foo.flow.json", 120_000)
    assert info["blocked"] is True
    assert len(info["next_allowed"]) >= 3
    print("alternatives_nonempty: OK")


if __name__ == "__main__":
    test_json_log_paths()
    test_block_full_read_json()
    test_allow_range_read()
    test_alternatives_nonempty()
    print("all passed")
