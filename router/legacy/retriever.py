"""Retrieve artifact raw content on demand (step 5)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from context_budget import truncate_to_token_budget
from context_need import ContextNeed
from runtime_core.evidence_keys import normalize_path
from runtime_core.evidence_cluster import extract_symbol_slice, should_skip_full_read
from legacy.memory_store import (
    ARTIFACT_DIR,
    Artifact,
    RequestDelta,
    SessionState,
    project_paths,
)

LOG = logging.getLogger("router.retriever")


@dataclass
class RetrievedChunk:
    artifact_id: str
    type: str
    name: str
    path: str
    score: float
    content: str
    chars: int


def _load_artifact_json(path: Path) -> Artifact | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Artifact(**{k: data[k] for k in Artifact.__dataclass_fields__ if k in data})
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def _artifact_search_paths(project_key: str, artifact_id: str) -> list[Path]:
    paths = [ARTIFACT_DIR / f"{artifact_id}.json"]
    if project_key and project_key != "unknown":
        paths.insert(0, project_paths(project_key).artifact_dir / f"{artifact_id}.json")
    return paths


def load_artifact_meta(artifact_id: str, project_key: str = "") -> Artifact | None:
    for path in _artifact_search_paths(project_key, artifact_id):
        art = _load_artifact_json(path)
        if art:
            return art
    return None


def _load_raw_text(art: Artifact) -> str:
    if art.raw_path:
        p = Path(art.raw_path)
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
    fallback = ARTIFACT_DIR / f"{art.artifact_id}.txt"
    if fallback.exists():
        return fallback.read_text(encoding="utf-8", errors="replace")
    return ""


def _prompt_content(art: Artifact, budget_tokens: int, *, phase: str = "") -> str:
    from artifact_excerpt import artifact_prompt_text

    return artifact_prompt_text(art, budget_tokens, phase=phase)


def _score_artifact(art: Artifact, query: str, delta: RequestDelta) -> float:
    q = query.lower()
    score = 0.0
    for term in art.index_terms:
        if term.lower() in q or term in query:
            score += 2.0
    if art.path and art.path.lower() in q:
        score += 3.0
    if art.name.lower() in q:
        score += 1.5
    for dm in delta.added:
        for ref in dm.file_refs:
            if art.path and ref in art.path:
                score += 4.0
            if ref in art.summary:
                score += 2.0
    if art.type == "shell_result" and any(
        kw in q for kw in ("docker", "로그", "log", "확인", "status", "ps")
    ):
        score += 1.0
    if art.type == "file_read" and any(
        kw in q for kw in ("파일", "read", "구조", "코드", "router", "memory")
    ):
        score += 0.8
    # Prefer recent artifacts
    if art.artifact_id:
        score += 0.1
    return score


def retrieve_artifacts(
    state: SessionState,
    query: str,
    delta: RequestDelta,
    budget_tokens: int,
    *,
    max_items: int = 4,
) -> list[RetrievedChunk]:
    if budget_tokens <= 0 or not state.artifacts:
        return []

    candidates: list[tuple[float, Artifact]] = []
    seen: set[str] = set()
    for aid in reversed(state.artifacts):
        if aid in seen:
            continue
        seen.add(aid)
        art = load_artifact_meta(aid, state.project_key)
        if not art:
            continue
        sc = _score_artifact(art, query, delta)
        if sc <= 0 and len(candidates) >= max_items:
            continue
        candidates.append((sc, art))

    candidates.sort(key=lambda x: x[0], reverse=True)
    chunks: list[RetrievedChunk] = []
    remaining = budget_tokens

    for score, art in candidates[: max_items * 2]:
        if len(chunks) >= max_items or remaining <= 128:
            break
        if score <= 0 and chunks:
            break
        if score <= 0 and chunks:
            break
        item_budget = min(remaining, max(256, budget_tokens // max_items))
        content = _prompt_content(art, item_budget)
        if not content.strip():
            continue
        chunks.append(
            RetrievedChunk(
                artifact_id=art.artifact_id,
                type=art.type,
                name=art.name,
                path=art.path,
                score=score,
                content=content,
                chars=len(content),
            )
        )
        remaining -= max(1, len(content) // 3)

    if chunks:
        LOG.info(
            "retriever project=%s hits=%d budget=%d query=%r",
            state.project_key,
            len(chunks),
            budget_tokens,
            query[:80],
        )
    return chunks


def format_retrieved(chunks: list[RetrievedChunk]) -> str:
    if not chunks:
        return ""
    lines = ["[retrieved_context]"]
    for ch in chunks:
        header = f"- {ch.type}/{ch.name}"
        if ch.path:
            header += f" path={ch.path}"
        lines.append(header)
        lines.append(ch.content)
        lines.append("")
    lines.append("[/retrieved_context]")
    return "\n".join(lines).strip()


@dataclass
class RetrievalItem:
    source: str
    tokens: int
    score: float
    section: str = ""
    must_include: bool = False
    artifact_id: str = ""
    content: str = ""


@dataclass
class RetrievalPack:
    items: list[RetrievalItem]
    total_tokens: int = 0
    missing_targets: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.total_tokens and self.items:
            self.total_tokens = sum(i.tokens for i in self.items)


def estimate_chunk_tokens(text: str) -> int:
    return max(1, len(text or "") // 3)


def rank_by_need(
    candidates: list[tuple[float, Artifact]],
    need: ContextNeed,
    query: str,
) -> list[tuple[float, Artifact]]:
    q = query.lower()
    targets = [t.lower() for t in (need.coverage_targets or [])]

    def boost(score: float, art: Artifact) -> float:
        path = (art.path or "").lower()
        name = (art.name or "").lower()
        for t in targets:
            if t in path or t in name or t in q:
                score += 5.0
        if "retrieved_code" in need.required_sources and art.type == "file_read":
            score += 1.5
        if "tool_result" in need.required_sources and art.type in ("tool_result", "shell_result"):
            score += 1.0
        return score

    ranked = [(boost(sc, art), art) for sc, art in candidates]
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked


def retrieve_for_need(
    state: SessionState,
    query: str,
    delta: RequestDelta,
    need: ContextNeed,
    budget_tokens: int,
    *,
    max_items: int = 6,
    force_refresh: bool = False,
    skip_full_read_paths: list[str] | None = None,
    prefer_symbols: list[str] | None = None,
    reuse_artifact_ids: list[str] | None = None,
    phase: str = "",
) -> RetrievalPack:
    """Retrieval-first pack — ranked by ContextNeed, measured in tokens."""
    _ = force_refresh
    workspace = getattr(state, "workspace_path", "") or ""
    skip_paths = {normalize_path(p, workspace) for p in (skip_full_read_paths or [])}
    symbols = [s.lower() for s in (prefer_symbols or []) if s]
    reuse_ids = set(reuse_artifact_ids or [])
    targets = [t.lower() for t in (need.coverage_targets or [])]
    if phase in ("final_answer", "partial_final_answer", "recovery_final"):
        max_items = max(max_items, len(targets) * 3, 12, min(len(state.artifacts or []), 24))
    if budget_tokens <= 0 or not state.artifacts:
        missing = list(need.coverage_targets or [])
        return RetrievalPack(items=[], total_tokens=0, missing_targets=missing)

    candidates: list[tuple[float, Artifact]] = []
    seen: set[str] = set()
    current_chat = getattr(state, "chat_id", "") or ""
    for aid in reversed(state.artifacts):
        if aid in seen:
            continue
        seen.add(aid)
        art = load_artifact_meta(aid, state.project_key)
        if not art:
            continue
        art_chat = getattr(art, "chat_id", "") or ""
        if current_chat and art_chat and art_chat != current_chat:
            continue
        sc = _score_artifact(art, query, delta)
        candidates.append((sc, art))

    ranked = rank_by_need(candidates, need, query)
    items: list[RetrievalItem] = []
    remaining = budget_tokens
    hit_targets: set[str] = set()
    seen_sources: set[str] = set()
    seen_artifact_ids: set[str] = set()

    try:
        from reference.target_coverage import coverage_target_in_text
    except ImportError:
        coverage_target_in_text = None  # type: ignore[assignment]

    def _target_hit(t: str, source: str, content: str) -> bool:
        if coverage_target_in_text:
            return coverage_target_in_text(t, source) or coverage_target_in_text(t, content)
        return t in source.lower() or t in content.lower()

    def _append_item(score: float, art: Artifact, *, must: bool = False) -> bool:
        nonlocal remaining
        if len(items) >= max_items or remaining <= 64:
            return False
        if art.artifact_id in seen_artifact_ids:
            return False
        per_target_floor = max(512, budget_tokens // max(1, len(targets) or max_items))
        if phase in ("final_answer", "partial_final_answer", "recovery_final"):
            per_target_floor = max(1024, budget_tokens // max(1, len(targets) or max(1, max_items // 2)))
        item_budget = min(remaining, max(per_target_floor, budget_tokens // max(1, max_items)))
        art_norm = normalize_path(art.path or art.name or "", workspace)
        skip_full = art_norm in skip_paths or (
            art.type == "file_read" and should_skip_full_read(state, art.path or "", workspace=workspace)[0]
        )
        if skip_full:
            try:
                from runtime_core.evidence_cluster import record_avoided_full_read

                record_avoided_full_read(state, art.path or art.name or "", workspace=workspace, reason="recovery_excerpt_reuse")
            except ImportError:
                pass
        content = _prompt_content(art, item_budget, phase=phase)
        if not content.strip():
            return False
        source = art.path or art.name or art.artifact_id
        source_key = source.lower()
        if source_key in seen_sources and phase not in ("final_answer", "partial_final_answer", "recovery_final"):
            return False
        tokens = estimate_chunk_tokens(content)
        if tokens > remaining and tokens > per_target_floor:
            content = truncate_to_token_budget(content, remaining)
            tokens = estimate_chunk_tokens(content)
        if tokens <= 0:
            return False
        for t in targets:
            if _target_hit(t, source, content):
                hit_targets.add(t)
        items.append(
            RetrievalItem(
                source=source,
                tokens=tokens,
                score=score + (5.0 if art.artifact_id in reuse_ids else 0.0),
                section=art.type,
                must_include=must
                or any(_target_hit(t, source, content) for t in targets),
                artifact_id=art.artifact_id,
                content=content,
            )
        )
        seen_sources.add(source_key)
        seen_artifact_ids.add(art.artifact_id)
        remaining -= tokens
        return True

    # final_answer: one artifact per coverage target first
    if phase in ("final_answer", "partial_final_answer", "recovery_final") and targets:
        for t in targets:
            best: tuple[float, Artifact] | None = None
            for score, art in ranked:
                source = art.path or art.name or art.artifact_id
                if _target_hit(t, source, "") or _target_hit(t, source, art.summary or ""):
                    if best is None or score > best[0]:
                        best = (score, art)
            if best:
                _append_item(best[0] + 10.0, best[1], must=True)

    # Optional vector retrieval (LlamaIndex or builtin BM25)
    try:
        from integrations.llamaindex import vector_retrieval_enabled, vector_retrieve

        if vector_retrieval_enabled():
            for vh in vector_retrieve(state, query, delta, top_k=max_items):
                source = vh.get("source") or vh.get("artifact_id") or "vector"
                if source in seen_sources:
                    continue
                text = vh.get("text") or ""
                if not text.strip():
                    continue
                item_budget = min(remaining, max(256, budget_tokens // max(1, max_items)))
                content = truncate_to_token_budget(text, item_budget)
                tokens = estimate_chunk_tokens(content)
                if tokens > remaining:
                    continue
                must = any(t in source.lower() for t in targets)
                for t in targets:
                    if t in source.lower() or t in content.lower():
                        hit_targets.add(t)
                items.append(
                    RetrievalItem(
                        source=source,
                        tokens=tokens,
                        score=float(vh.get("score", 0)) + 10.0,
                        section=f"vector:{vh.get('backend', 'bm25')}",
                        must_include=must,
                        artifact_id=vh.get("artifact_id", ""),
                        content=content,
                    )
                )
                seen_sources.add(source)
                remaining -= tokens
                if len(items) >= max_items or remaining <= 128:
                    break
    except ImportError:
        pass

    for score, art in ranked[: max_items * 4]:
        if len(items) >= max_items or remaining <= 64:
            break
        if score <= 0 and items and phase not in ("final_answer", "partial_final_answer", "recovery_final"):
            break
        if reuse_ids and art.artifact_id not in reuse_ids and skip_paths:
            continue
        _append_item(score, art)

    missing = [t for t in (need.coverage_targets or []) if t.lower() not in hit_targets]
    total = sum(i.tokens for i in items)
    pack = RetrievalPack(items=items, total_tokens=total, missing_targets=missing)

    if items:
        LOG.info(
            "retrieve_for_need intent=%s items=%d tokens=%d missing=%d",
            need.intent,
            len(items),
            total,
            len(missing),
        )
    return pack


def format_retrieval_pack(pack: RetrievalPack) -> str:
    if not pack.items:
        return ""
    lines = ["[retrieved_context]"]
    for item in pack.items:
        header = f"- {item.source}"
        if item.section:
            header += f" ({item.section}, score={item.score:.2f}, tokens={item.tokens})"
        lines.append(header)
        lines.append(item.content)
        lines.append("")
    lines.append("[/retrieved_context]")
    return "\n".join(lines).strip()


def pack_to_chunks(pack: RetrievalPack) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            artifact_id=item.artifact_id,
            type=item.section or "artifact",
            name=item.source.rsplit("/", 1)[-1],
            path=item.source,
            score=item.score,
            content=item.content,
            chars=len(item.content),
        )
        for item in pack.items
    ]

