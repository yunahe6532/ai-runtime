#!/usr/bin/env python3
"""Vector retrieval E2E — real artifact corpus + BM25 / LlamaIndex integration."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "router"
sys.path.insert(0, str(ROUTER))

DEFAULT_CACHE = ROOT / "tmp" / "context-cache"
DEFAULT_PROJECT = "e5903e2a81c2"

os.environ.setdefault("CONTEXT_CACHE_DIR", str(DEFAULT_CACHE))
os.environ.setdefault("VECTOR_RETRIEVAL", "1")
os.environ.setdefault("MEMORY_STORE", "1")

from context_need import build_context_need  # noqa: E402
from integrations.llamaindex import (  # noqa: E402
    build_document_corpus,
    vector_retrieve,
)
from adapters.memory import RequestDelta, load_state  # noqa: E402
from reference.planner import AgentPlan  # noqa: E402
from adapters.retrieval import retrieve_for_need  # noqa: E402

DELTA = RequestDelta(
    delta_id="e2e_delta",
    req_id="e2e_req",
    prev_req_id=None,
    prev_message_count=0,
    curr_message_count=1,
    added_count=0,
)

# query → substring expected in top-1 source (case-insensitive)
QUERY_CASES = [
    ("benchmark runtime score token_threshold", "benchmark"),
    ("agent_exec tool validation guard", "agent_exec"),
    ("context budget allocate dynamic", "context"),
    ("docker shell compose status", "docker"),
    ("flow trace proxy saved percent", "flow"),
]


def _backend() -> str:
    if os.getenv("LLAMAINDEX_ENABLED", "1") == "1":
        try:
            import llama_index.core  # noqa: F401

            return "llamaindex"
        except ImportError:
            pass
    return "builtin_bm25"


def load_corpus_state(project_key: str) -> tuple[object, int]:
    state = load_state(project_key)
    if not state.project_key:
        state.project_key = project_key
    corpus = build_document_corpus(state, "benchmark", DELTA)
    return state, len(corpus)


def test_corpus_loaded(project_key: str) -> None:
    state, n = load_corpus_state(project_key)
    assert n >= 10, f"corpus too small: {n} docs (project={project_key})"
    assert len(state.artifacts or []) >= 10
    print(f"corpus_loaded: OK docs={n} artifacts={len(state.artifacts)}")


def test_vector_retrieve_queries(project_key: str) -> None:
    state, _ = load_corpus_state(project_key)
    backend = _backend()
    passed = 0
    for query, expect in QUERY_CASES:
        hits = vector_retrieve(state, query, DELTA, top_k=3)
        assert hits, f"no hits for query={query!r}"
        top = hits[0]
        src = (top.get("source") or "").lower()
        assert expect.lower() in src or expect.lower() in (top.get("text") or "").lower(), (
            f"query={query!r} top={top.get('source')} expected~{expect}"
        )
        assert top.get("backend") == backend, f"backend mismatch want={backend} got={top.get('backend')}"
        passed += 1
        print(f"  vector_query OK expect={expect} top={Path(top['source']).name[:50]} score={top['score']:.3f}")
    print(f"vector_retrieve_queries: OK {passed}/{len(QUERY_CASES)} backend={backend}")


def test_retrieve_for_need_with_vector(project_key: str) -> None:
    os.environ["VECTOR_RETRIEVAL"] = "1"
    state, _ = load_corpus_state(project_key)
    need = build_context_need(AgentPlan(task_intent="architecture"), "benchmark runtime score analysis", "agent")
    pack_off = retrieve_for_need(state, "benchmark runtime score", DELTA, need, 4000)
    os.environ["VECTOR_RETRIEVAL"] = "0"
    pack_no_vec = retrieve_for_need(state, "benchmark runtime score", DELTA, need, 4000)

    vector_items = [i for i in pack_off.items if str(i.section).startswith("vector:")]
    assert pack_off.total_tokens > 0, "vector retrieval pack empty"
    assert vector_items, f"no vector: items in pack sections={[i.section for i in pack_off.items[:5]]}"
    print(
        "retrieve_for_need_vector: OK",
        f"items={len(pack_off.items)} vector_items={len(vector_items)}",
        f"tokens={pack_off.total_tokens}",
        f"delta_vs_no_vector={pack_off.total_tokens - pack_no_vec.total_tokens}",
    )


def test_bm25_vs_llamaindex_agreement(project_key: str) -> None:
    """When both backends available, top-1 should agree on same artifact family."""
    try:
        import llama_index.core  # noqa: F401
    except ImportError:
        print("bm25_vs_llamaindex: SKIP (llama-index not installed)")
        return

    state, _ = load_corpus_state(project_key)
    query = "benchmark runtime score p1 log"

    os.environ["LLAMAINDEX_ENABLED"] = "0"
    bm25_hits = vector_retrieve(state, query, DELTA, top_k=1)

    os.environ["LLAMAINDEX_ENABLED"] = "1"
    llm_hits = vector_retrieve(state, query, DELTA, top_k=1)

    assert bm25_hits and llm_hits
    b_src = Path(bm25_hits[0]["source"]).name.lower()
    l_src = Path(llm_hits[0]["source"]).name.lower()
    # same file or both benchmark-related
    agree = b_src == l_src or ("benchmark" in b_src and "benchmark" in l_src)
    assert agree, f"top mismatch bm25={b_src} llamaindex={l_src}"
    print(f"bm25_vs_llamaindex: OK bm25={b_src} llamaindex={l_src}")


def main() -> int:
    project = os.getenv("VECTOR_E2E_PROJECT", DEFAULT_PROJECT)
    cache = Path(os.getenv("CONTEXT_CACHE_DIR", str(DEFAULT_CACHE)))
    if not cache.exists():
        print(f"SKIP: cache dir missing {cache}")
        return 1
    state_path = cache / "projects" / project / "current_state.json"
    if not state_path.exists():
        print(f"SKIP: project state missing {state_path}")
        return 1

    print(f"vector_retrieval_e2e project={project} cache={cache} backend={_backend()}")
    test_corpus_loaded(project)
    test_vector_retrieve_queries(project)
    test_retrieve_for_need_with_vector(project)
    test_bm25_vs_llamaindex_agreement(project)
    print("ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
