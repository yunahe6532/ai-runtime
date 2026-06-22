#!/usr/bin/env python3
"""Tests for runtime_core.indexing_helpers (replaces legacy context_optimizer)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

from runtime_core.indexing_helpers import (  # noqa: E402
    Section,
    _extract_user_query,
    classify_message,
)


def test_classify_user_query():
    msg = {"role": "user", "content": "<user_query>fix bug</user_query>"}
    assert classify_message(msg, 5) == Section.USER_QUERY
    print("classify_user_query: OK")


def test_extract_user_query():
    text = "<user_query>hello world</user_query>"
    assert _extract_user_query(text) == "hello world"
    print("extract_user_query: OK")


if __name__ == "__main__":
    test_classify_user_query()
    test_extract_user_query()
    print("ALL OK")
