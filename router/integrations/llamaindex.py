"""Vector retrieval adapter — LlamaIndex when installed, builtin BM25 fallback."""

from __future__ import annotations

import logging
import math
import os
import re
from collections import Counter
from typing import Any

LOG = logging.getLogger("router.integrations.llamaindex")

VECTOR_RETRIEVAL = os.getenv("VECTOR_RETRIEVAL", "0") == "1"
LLAMAINDEX_ENABLED = os.getenv("LLAMAINDEX_ENABLED", "1") == "1"
_VECTOR_TOP_K = int(os.getenv("VECTOR_TOP_K", "6"))


def vector_retrieval_enabled() -> bool:
    return VECTOR_RETRIEVAL


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9_\uac00-\ud7a3]{2,}", (text or "").lower())


def _bm25_score(query_tokens: list[str], doc_tokens: list[str], avg_dl: float, df: dict[str, int], n_docs: int) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    k1, b = 1.2, 0.75
    dl = len(doc_tokens)
    tf = Counter(doc_tokens)
    score = 0.0
    for term in query_tokens:
        if term not in tf:
            continue
        idf = math.log(1 + (n_docs - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
        freq = tf[term]
        score += idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * dl / max(avg_dl, 1)))
    return score


def _builtin_vector_search(
    documents: list[dict[str, Any]],
    query: str,
    *,
    top_k: int = 6,
) -> list[tuple[float, dict[str, Any]]]:
    """Lightweight BM25 over artifact text — no external deps."""
    if not documents or not query.strip():
        return []
    q_tokens = _tokenize(query)
    doc_tokens_list = [_tokenize(d.get("text", "")) for d in documents]
    n = len(documents)
    avg_dl = sum(len(t) for t in doc_tokens_list) / max(n, 1)
    df: dict[str, int] = {}
    for tokens in doc_tokens_list:
        for t in set(tokens):
            df[t] = df.get(t, 0) + 1
    scored = [
        (_bm25_score(q_tokens, tokens, avg_dl, df, n), doc)
        for doc, tokens in zip(documents, doc_tokens_list)
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [(s, d) for s, d in scored[:top_k] if s > 0]


def _llamaindex_search(
    documents: list[dict[str, Any]],
    query: str,
    *,
    top_k: int = 6,
) -> list[tuple[float, dict[str, Any]]]:
    try:
        from llama_index.core import Document, VectorStoreIndex
        from llama_index.core.node_parser import SentenceSplitter

        docs = [
            Document(text=d["text"], metadata={"source": d.get("source", ""), "artifact_id": d.get("artifact_id", "")})
            for d in documents
            if d.get("text")
        ]
        if not docs:
            return []
        index = VectorStoreIndex.from_documents(docs, transformations=[SentenceSplitter(chunk_size=512)])
        retriever = index.as_retriever(similarity_top_k=min(top_k, len(docs)))
        nodes = retriever.retrieve(query)
        out: list[tuple[float, dict[str, Any]]] = []
        for node in nodes:
            meta = node.metadata or {}
            out.append((
                float(getattr(node, "score", 0) or 0),
                {
                    "source": meta.get("source", ""),
                    "artifact_id": meta.get("artifact_id", ""),
                    "text": node.get_content(),
                },
            ))
        return out
    except ImportError:
        LOG.debug("llama-index not installed — using builtin BM25")
        return _builtin_vector_search(documents, query, top_k=top_k)
    except Exception as exc:
        LOG.warning("llamaindex search failed: %s — fallback BM25", exc)
        return _builtin_vector_search(documents, query, top_k=top_k)


def build_document_corpus(state: Any, query: str, delta: Any) -> list[dict[str, Any]]:
    """Build searchable corpus from session artifacts."""
    from legacy.retriever import _load_raw_text, _score_artifact, load_artifact_meta

    docs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for aid in reversed(getattr(state, "artifacts", None) or []):
        if aid in seen:
            continue
        seen.add(aid)
        art = load_artifact_meta(aid, getattr(state, "project_key", ""))
        if not art:
            continue
        raw = _load_raw_text(art)
        from artifact_excerpt import artifact_prompt_text

        text = artifact_prompt_text(art, 0) or raw.strip()
        if not text:
            continue
        source = art.path or art.name or art.artifact_id
        base_score = _score_artifact(art, query, delta)
        docs.append({
            "artifact_id": art.artifact_id,
            "source": source,
            "text": text,
            "section": art.type,
            "base_score": base_score,
        })
    return docs


def vector_retrieve(
    state: Any,
    query: str,
    delta: Any,
    *,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """Return ranked vector hits: [{source, artifact_id, text, score, backend}]."""
    if not vector_retrieval_enabled():
        return []
    top_k = top_k or _VECTOR_TOP_K
    corpus = build_document_corpus(state, query, delta)
    if not corpus:
        return []

    if LLAMAINDEX_ENABLED:
        hits = _llamaindex_search(corpus, query, top_k=top_k)
        backend = "llamaindex"
    else:
        hits = _builtin_vector_search(corpus, query, top_k=top_k)
        backend = "builtin_bm25"

    results: list[dict[str, Any]] = []
    for score, doc in hits:
        results.append({
            "source": doc.get("source", ""),
            "artifact_id": doc.get("artifact_id", ""),
            "text": doc.get("text", ""),
            "score": float(score),
            "backend": backend,
        })
    if results:
        LOG.info("vector_retrieve backend=%s hits=%d query=%r", backend, len(results), query[:60])
    return results
