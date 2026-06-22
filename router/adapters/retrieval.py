"""Retrieval adapter — public entry: ``retrieve_for_need`` only.

Engine internals live in ``legacy/retriever.py`` + ``integrations/llamaindex.py``.
"""

from __future__ import annotations

from typing import Any

from legacy.retriever import (
    RetrievalItem,
    RetrievalPack,
    estimate_chunk_tokens,
    load_artifact_meta,
    retrieve_for_need as _retrieve,
)

__all__ = [
    "RetrievalItem",
    "RetrievalPack",
    "estimate_chunk_tokens",
    "load_artifact_meta",
    "retrieve_for_need",
]


def retrieve_for_need(*args: Any, **kwargs: Any) -> RetrievalPack:
    return _retrieve(*args, **kwargs)
