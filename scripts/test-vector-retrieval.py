#!/usr/bin/env python3
"""Test vector retrieval adapter (builtin BM25 + optional LlamaIndex)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

os.environ["VECTOR_RETRIEVAL"] = "1"
os.environ["LLAMAINDEX_ENABLED"] = "0"  # force builtin for CI

from integrations.llamaindex import _builtin_vector_search, vector_retrieve  # noqa: E402
from adapters.memory import Artifact, RequestDelta, SessionState  # noqa: E402


def test_builtin_bm25():
    docs = [
        {"text": "def allocate_dynamic budget context need", "source": "context_budget.py", "artifact_id": "a1"},
        {"text": "hello world unrelated", "source": "other.txt", "artifact_id": "a2"},
    ]
    hits = _builtin_vector_search(docs, "allocate dynamic budget", top_k=2)
    assert hits[0][1]["source"] == "context_budget.py"
    print("builtin_bm25: OK", hits[0][0])


def test_vector_retrieve_empty():
    state = SessionState()
    delta = RequestDelta(delta_id="d", req_id="r", prev_req_id=None, prev_message_count=0, curr_message_count=0, added_count=0)
    hits = vector_retrieve(state, "budget", delta)
    assert hits == []
    print("vector_retrieve_empty: OK")


if __name__ == "__main__":
    test_builtin_bm25()
    test_vector_retrieve_empty()
    print("ALL OK")
