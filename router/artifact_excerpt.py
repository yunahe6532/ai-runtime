"""Artifact excerpts for LLM prompts — rule chunk/merge + optional fast LLM 1-pass per chunk."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from context_budget import truncate_to_token_budget

LOG = logging.getLogger("router.artifact_excerpt")


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 3)


DEFAULT_INGEST_EXCERPT_CHARS = int(os.getenv("ARTIFACT_INGEST_EXCERPT_CHARS", "48000"))
CHUNK_FILE_LIMIT = int(os.getenv("ARTIFACT_CHUNK_FILES", "35"))
CHUNK_LINE_LIMIT = int(os.getenv("ARTIFACT_CHUNK_LINES", "400"))
ARTIFACT_LLM_MIN_RAW_CHARS = int(os.getenv("ARTIFACT_LLM_MIN_RAW_CHARS", "3500"))
ARTIFACT_LLM_MIN_CHUNK_CHARS = int(os.getenv("ARTIFACT_LLM_MIN_CHUNK_CHARS", "800"))
ARTIFACT_LLM_FINAL_MIN_CHUNK_CHARS = int(os.getenv("ARTIFACT_LLM_FINAL_MIN_CHUNK_CHARS", "120"))
ARTIFACT_LLM_MAX_TOKENS = int(os.getenv("ARTIFACT_LLM_MAX_TOKENS", "512"))
ARTIFACT_FINAL_EXCERPT_CHARS = int(os.getenv("ARTIFACT_FINAL_EXCERPT_CHARS", "96000"))
TIER_DIR_NAMES = ("runtime_core", "adapters", "legacy", "integrations")
_FINAL_EXCERPT_CACHE: dict[str, str] = {}
_TIER_MERGE_CACHE: dict[str, str] = {}


def clear_artifact_excerpt_cache() -> None:
    """Per-request cache — call at turn start to avoid duplicate LLM summarizes."""
    _FINAL_EXCERPT_CACHE.clear()
    _TIER_MERGE_CACHE.clear()


def _cache_key(art: Any, budget_tokens: int, phase: str) -> str:
    aid = str(getattr(art, "artifact_id", "") or getattr(art, "path", "") or id(art))
    return f"{aid}:{budget_tokens}:{phase}"


def _tier_key_from_path(path: str) -> str:
    p = str(path or "").replace("\\", "/").lower()
    for tier in TIER_DIR_NAMES:
        if f"/{tier}" in p or p.rstrip("/").endswith(tier):
            return tier
    return "other"


def llm_merge_tier_digests(body: str, *, budget_tokens: int = 0) -> str:
    """One-pass merge summarize when combined tier digests fit budget."""
    stripped = (body or "").strip()
    if not stripped or not _llm_summarize_enabled():
        return stripped
    est = _estimate_tokens(stripped)
    if budget_tokens > 0 and est > int(budget_tokens * 0.92):
        return stripped
    cache_key = f"merge:{hash(stripped) & 0xFFFFFF}:{budget_tokens}"
    cached = _TIER_MERGE_CACHE.get(cache_key)
    if cached:
        return cached
    merged = llm_summarize_one_chunk(
        stripped,
        path="read_only_tiers",
        chunk_idx=1,
        total=1,
        kind="tier_merge",
        min_chunk_chars=120,
    )
    if merged.strip() and merged != stripped:
        LOG.info(
            "tier_digest_merge ok tiers=%d in_chars=%d out_chars=%d budget=%d",
            stripped.count("## tier:"),
            len(stripped),
            len(merged),
            budget_tokens,
        )
        _TIER_MERGE_CACHE[cache_key] = merged
        return merged
    return stripped


def pack_tier_evidence_for_final(
    artifacts: list[Any],
    budget_tokens: int,
    *,
    phase: str = "final_answer",
    coverage_targets: list[str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Pick one digest per tier, merge if budget allows (VISION hierarchy)."""
    if budget_tokens <= 0 or not artifacts:
        return "", {}

    by_tier: dict[str, Any] = {}
    for art in artifacts:
        if getattr(art, "is_error", False):
            continue
        if getattr(art, "type", "") not in ("file_read", "tool_result", "shell_result"):
            continue
        tier = _tier_key_from_path(getattr(art, "path", "") or getattr(art, "name", ""))
        if tier == "other":
            continue
        prev = by_tier.get(tier)
        if prev is None or int(getattr(art, "chars", 0) or 0) > int(getattr(prev, "chars", 0) or 0):
            by_tier[tier] = art

    if not by_tier:
        return "", {}

    tier_order = [t for t in TIER_DIR_NAMES if t in by_tier]
    tier_order.extend(sorted(t for t in by_tier if t not in tier_order))
    n = max(1, len(tier_order))
    per_tier = max(512, budget_tokens // n)
    digests: dict[str, str] = {}
    quote_banks: dict[str, str] = {}
    for tier in tier_order:
        art = by_tier[tier]
        text = artifact_prompt_text(art, per_tier, phase=phase)
        quotes = _quote_bank_from_artifact(art)
        if text.strip():
            digests[tier] = text.strip()
        if quotes.strip():
            quote_banks[tier] = quotes

    if not digests:
        return "", {}

    parts = []
    for tier in tier_order:
        body = digests.get(tier, "")
        if not body:
            continue
        block = f"## tier:{tier}\n{body}"
        qb = quote_banks.get(tier, "")
        if qb:
            block = f"{block}\n\n{qb}"
        parts.append(block)
    combined = "\n\n".join(parts)
    combined_est = _estimate_tokens(combined)
    if combined_est <= int(budget_tokens * 0.88) and len(digests) >= 2:
        merged = llm_merge_tier_digests(combined, budget_tokens=budget_tokens)
        block = f"[tier_evidence_merged tiers={len(digests)}]\n{merged}\n[/tier_evidence_merged]"
        LOG.info(
            "tier_evidence_pack tiers=%d budget=%d used_est=%d merged=%s",
            len(digests),
            budget_tokens,
            _estimate_tokens(block),
            str(merged != combined).lower(),
        )
        return block, digests

    out_parts = ["[collected_evidence_by_tier]"]
    remaining = budget_tokens
    for tier in tier_order:
        body = digests.get(tier, "")
        if not body or remaining <= 128:
            continue
        chunk = truncate_to_token_budget(body, min(per_tier, remaining))
        out_parts.append(f"## tier:{tier}")
        out_parts.append(chunk)
        remaining -= _estimate_tokens(chunk)
    out_parts.append("[/collected_evidence_by_tier]")
    block = "\n\n".join(out_parts).strip()
    LOG.info(
        "tier_evidence_pack tiers=%d budget=%d used_est=%d merged=false",
        len(digests),
        budget_tokens,
        budget_tokens - max(0, remaining),
    )
    return block, digests


def _llm_summarize_enabled() -> bool:
    return os.getenv("ARTIFACT_LLM_SUMMARIZE", "1").strip().lower() not in ("0", "false", "no", "off")


@dataclass
class _FileHit:
    path: str
    lines: list[str] = field(default_factory=list)

    def doc_hint(self) -> str:
        for ln in self.lines[:12]:
            m = re.search(r"^\s*\d+:(.*)$", ln)
            if not m:
                continue
            body = m.group(1).strip()
            if body.startswith(('"""', "'''")):
                return body.strip("\"'")[:240]
            if body.startswith("#") and len(body) > 2:
                return body.lstrip("# ").strip()[:240]
            if '"""' in body or "'''" in body:
                return body[:240]
        return ""


def count_grep_files(text: str) -> int:
    """Count distinct file headers in a ripgrep workspace_result blob."""
    return len(_parse_grep_workspace(text or ""))


def _parse_grep_workspace(text: str) -> list[_FileHit]:
    lines = text.splitlines()
    hits: list[_FileHit] = []
    current: _FileHit | None = None
    file_re = re.compile(
        r"^([\w./~-]+(?:\.py|\.md|\.yml|\.yaml|\.json|\.ts|\.tsx|\.js|\.sh|\.toml))(?:\s+\(no\s+matches\))?$",
        re.I,
    )
    for ln in lines:
        if ln.strip().startswith("<workspace_result"):
            continue
        if ln.strip() == "</workspace_result>":
            continue
        fm = file_re.match(ln.strip())
        if fm:
            if current and current.lines:
                hits.append(current)
            current = _FileHit(path=fm.group(1).replace("\\", "/"))
            continue
        if current is not None and re.match(r"^\s*\d+:", ln):
            current.lines.append(ln)
    if current and (current.lines or current.path):
        hits.append(current)
    return hits


def _excerpt_grep_files(files: list[_FileHit], *, header: str = "") -> str:
    rows: list[str] = []
    if header:
        rows.append(header)
    for f in files:
        hint = f.doc_hint()
        if hint:
            rows.append(f"- {f.path}: {hint}")
        else:
            rows.append(f"- {f.path}")
    return "\n".join(rows)


def extract_grep_quote_bank(text: str, *, max_files: int = 14, max_lines_per_file: int = 4) -> str:
    """Preserve path:line citations from ripgrep output for final synthesis."""
    files = _parse_grep_workspace(text or "")
    if not files:
        return ""
    rows: list[str] = ["[quote_bank]"]
    for f in files[:max_files]:
        rows.append(f"{f.path}:")
        for ln in f.lines[:max_lines_per_file]:
            m = re.match(r"^\s*(\d+):(.*)$", ln)
            if m:
                rows.append(f"  L{m.group(1)}:{m.group(2).strip()[:240]}")
            elif ln.strip():
                rows.append(f"  {ln.strip()[:240]}")
    rows.append("[/quote_bank]")
    return "\n".join(rows)


def _quote_bank_from_artifact(art: Any, *, max_files: int = 14) -> str:
    raw = _load_raw_from_artifact(art)
    if not raw.strip() or "<workspace_result" not in raw[:1200]:
        return ""
    return extract_grep_quote_bank(raw, max_files=max_files)


def _chunk_list(items: list[Any], size: int) -> list[list[Any]]:
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


def _extract_completion_content(json_data: dict[str, Any] | None) -> str:
    if not json_data:
        return ""
    data = dict(json_data)
    try:
        from adapters.gateway import normalize_completion_response

        data = normalize_completion_response(data)
    except Exception:
        pass
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return str(msg.get("content") or "").strip()


def llm_summarize_one_chunk(
    chunk: str,
    *,
    path: str = "",
    chunk_idx: int = 1,
    total: int = 1,
    kind: str = "grep",
    min_chunk_chars: int | None = None,
) -> str:
    """Fast-backend 1-pass summary for one rule excerpt chunk."""
    min_len = ARTIFACT_LLM_MIN_CHUNK_CHARS if min_chunk_chars is None else min_chunk_chars
    if not chunk.strip() or len(chunk) < min_len:
        return chunk

    sys_prompt = (
        "Summarize this code-search tool output chunk for architecture analysis. "
        "Bullet list: exact file path + one-line role from docstrings or comments. "
        "When line numbers appear in input (e.g. 12:def foo), preserve as path L12: snippet. "
        "Preserve directory structure. Match source language. No tools. Prose only."
    )
    if kind == "tier_merge":
        sys_prompt = (
            "Merge multi-tier architecture evidence for a final cited answer. "
            "Keep ## tier sections. Preserve every path and L123: line citation from quote_bank. "
            "Keep import/boundary facts. Do not invent paths or lines not in input."
        )
    user = chunk[:14000]
    body = {
        "model": os.getenv("ARTIFACT_LLM_MODEL", "fast"),
        "stream": False,
        "temperature": 0.1,
        "max_tokens": ARTIFACT_LLM_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"[{kind} dir={path or '?'} chunk={chunk_idx}/{total}]\n\n{user}"},
        ],
    }
    try:
        import json

        from adapters.gateway import chat_completion

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        result = chat_completion(
            method="POST",
            path="/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            body_bytes=payload,
            body_json=body,
            backend_hint="fast",
            stream=False,
        )
        if getattr(result, "status_code", 500) != 200:
            LOG.warning(
                "artifact llm summarize status=%s chunk=%s/%s path=%s",
                getattr(result, "status_code", "?"),
                chunk_idx,
                total,
                path,
            )
            return chunk
        content = _extract_completion_content(result.json_data)
        if content:
            hdr = f"[llm summary {kind} dir={path or '?'} chunk={chunk_idx}/{total}]"
            LOG.info(
                "artifact llm summarize ok chunk=%s/%s in_chars=%d out_chars=%d path=%s",
                chunk_idx,
                total,
                len(chunk),
                len(content),
                path,
            )
            return f"{hdr}\n{content}"
    except Exception as exc:
        LOG.warning("artifact llm summarize failed chunk=%s/%s path=%s: %s", chunk_idx, total, path, exc)
    return chunk


def apply_llm_one_pass(
    chunks: list[str],
    *,
    path: str = "",
    raw_len: int = 0,
    kind: str = "grep",
    min_chunk_chars: int | None = None,
    force: bool = False,
) -> list[str]:
    """Run fast LLM summarize on each chunk when raw payload is large enough."""
    if not _llm_summarize_enabled() or not chunks:
        return chunks
    if not force and raw_len < ARTIFACT_LLM_MIN_RAW_CHARS and len(chunks) <= 1:
        return chunks
    total = len(chunks)
    return [
        llm_summarize_one_chunk(
            c,
            path=path,
            chunk_idx=i + 1,
            total=total,
            kind=kind,
            min_chunk_chars=min_chunk_chars,
        )
        for i, c in enumerate(chunks)
    ]


def build_glob_excerpt(text: str, *, path: str = "", max_chars: int = DEFAULT_INGEST_EXCERPT_CHARS) -> tuple[str, list[str]]:
    """Parse Glob tool output into file inventory excerpt."""
    lines = text.splitlines()
    paths: list[str] = []
    file_re = re.compile(
        r"([\w./~-]+\.(?:py|md|yml|yaml|json|ts|tsx|js|sh|toml))\s*$",
        re.I,
    )
    for ln in lines:
        s = ln.strip()
        if not s or s.lower().startswith("result of search") or s.lower().startswith("total "):
            continue
        m = file_re.search(s.replace("\\", "/"))
        if m:
            paths.append(m.group(1))
    if not paths:
        return _excerpt_plain_text(text, label="glob", path=path, max_chars=max_chars)

    chunks_out: list[str] = []
    file_chunks = _chunk_list(paths, CHUNK_FILE_LIMIT)
    for i, fc in enumerate(file_chunks):
        hdr = f"[glob excerpt dir={path or '?'} chunk={i + 1}/{len(file_chunks)} files={len(fc)}/{len(paths)}]"
        body = "\n".join(f"- {p}" for p in fc)
        chunks_out.append(f"{hdr}\n{body}")

    chunks_out = apply_llm_one_pass(chunks_out, path=path, raw_len=len(text), kind="glob")
    merged = _merge_chunks(chunks_out, max_chars=max_chars)
    return merged, chunks_out


def build_grep_excerpt(text: str, *, path: str = "", max_chars: int = DEFAULT_INGEST_EXCERPT_CHARS) -> tuple[str, list[str]]:
    """Parse ripgrep workspace_result; chunk by file; rule excerpt; optional LLM 1-pass; merge."""
    files = _parse_grep_workspace(text)
    if not files:
        return _excerpt_plain_text(text, label="grep", path=path, max_chars=max_chars)

    chunks_out: list[str] = []
    file_chunks = _chunk_list(files, CHUNK_FILE_LIMIT)
    for i, fc in enumerate(file_chunks):
        hdr = f"[grep excerpt dir={path or '?'} chunk={i + 1}/{len(file_chunks)} files={len(fc)}/{len(files)}]"
        part = _excerpt_grep_files(fc, header=hdr)
        chunks_out.append(part)

    chunks_out = apply_llm_one_pass(chunks_out, path=path, raw_len=len(text), kind="grep")
    merged = _merge_chunks(chunks_out, max_chars=max_chars)
    return merged, chunks_out


def _excerpt_plain_text(
    text: str,
    *,
    label: str,
    path: str = "",
    max_chars: int,
) -> tuple[str, list[str]]:
    lines = text.splitlines()
    if len(lines) <= CHUNK_LINE_LIMIT and len(text) <= max_chars:
        hdr = f"[{label} excerpt lines={len(lines)} chars={len(text)}]"
        body = f"{hdr}\n{text}"
        chunks_out = apply_llm_one_pass([body], path=path, raw_len=len(text), kind=label)
        return chunks_out[0] if chunks_out else body, chunks_out

    chunks_out: list[str] = []
    line_chunks = _chunk_list(lines, CHUNK_LINE_LIMIT)
    for i, lc in enumerate(line_chunks):
        body = "\n".join(lc)
        hdr = f"[{label} excerpt chunk={i + 1}/{len(line_chunks)} lines={len(lc)}]"
        chunks_out.append(f"{hdr}\n{body}")

    chunks_out = apply_llm_one_pass(chunks_out, path=path, raw_len=len(text), kind=label)
    merged = _merge_chunks(chunks_out, max_chars=max_chars)
    return merged, chunks_out


def build_file_read_excerpt(text: str, *, path: str = "", max_chars: int = DEFAULT_INGEST_EXCERPT_CHARS) -> tuple[str, list[str]]:
    return _excerpt_plain_text(text, label="read", path=path, max_chars=max_chars)


def build_shell_excerpt(text: str, *, path: str = "", max_chars: int = DEFAULT_INGEST_EXCERPT_CHARS) -> tuple[str, list[str]]:
    lines = text.splitlines()
    if len(text) <= max_chars:
        hdr = f"[shell excerpt lines={len(lines)}]"
        body = f"{hdr}\n{text}"
        chunks_out = apply_llm_one_pass([body], path=path, raw_len=len(text), kind="shell")
        return chunks_out[0] if chunks_out else body, chunks_out

    head = lines[:60]
    tail = lines[-20:] if len(lines) > 80 else []
    mid_skip = len(lines) - len(head) - len(tail)
    parts = [f"[shell excerpt lines={len(lines)} path={path or '?'}]"]
    parts.append("\n".join(head))
    if mid_skip > 0:
        parts.append(f"...({mid_skip} lines omitted)...")
    if tail:
        parts.append("\n".join(tail))
    body = "\n".join(parts)
    if len(body) > max_chars:
        body = body[: max_chars - 24] + "\n...(shell excerpt trimmed)"
    chunks_out = apply_llm_one_pass([body], path=path, raw_len=len(text), kind="shell")
    return (chunks_out[0] if chunks_out else body), chunks_out


def _merge_chunks(chunks: list[str], *, max_chars: int) -> str:
    out: list[str] = []
    used = 0
    for part in chunks:
        if not part.strip():
            continue
        if used + len(part) + 2 > max_chars:
            remain = max_chars - used - 40
            if remain > 200:
                out.append(part[:remain] + "\n...(excerpt merge cap)")
            break
        out.append(part)
        used += len(part) + 2
    return "\n\n".join(out)


def build_prompt_excerpt(
    text: str,
    *,
    path: str = "",
    tool_name: str = "",
    art_type: str = "",
    max_chars: int = DEFAULT_INGEST_EXCERPT_CHARS,
) -> tuple[str, list[str]]:
    """Build merged prompt excerpt + per-chunk KV list (rule + optional fast LLM 1-pass)."""
    stripped = (text or "").strip()
    if not stripped:
        return "", []

    name = (tool_name or "").lower()
    if name == "glob" or (art_type == "tool_result" and "result of search" in stripped[:400].lower()):
        return build_glob_excerpt(stripped, path=path, max_chars=max_chars)
    if "<workspace_result" in stripped[:800] or name == "grep" or art_type == "file_read" and "<workspace_result" in stripped[:2000]:
        return build_grep_excerpt(stripped, path=path, max_chars=max_chars)
    if art_type == "shell_result" or "Exit code:" in stripped[:400] or name == "shell":
        return build_shell_excerpt(stripped, path=path, max_chars=max_chars)
    return build_file_read_excerpt(stripped, path=path, max_chars=max_chars)


def _load_raw_from_artifact(art: Any) -> str:
    from pathlib import Path
    from legacy.memory_store import ARTIFACT_DIR

    raw_path = str(getattr(art, "raw_path", "") or "").strip()
    if raw_path:
        p = Path(raw_path)
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    aid = str(getattr(art, "artifact_id", "") or "")
    fallback = ARTIFACT_DIR / f"{aid}.txt"
    if aid and fallback.is_file():
        return fallback.read_text(encoding="utf-8", errors="replace")
    return ""


def rebuild_prompt_excerpt_from_artifact(art: Any, *, max_chars: int = DEFAULT_INGEST_EXCERPT_CHARS) -> tuple[str, list[str]]:
    raw = _load_raw_from_artifact(art)
    if not raw.strip():
        return "", []
    return build_prompt_excerpt(
        raw,
        path=str(getattr(art, "path", "") or ""),
        tool_name=str(getattr(art, "name", "") or ""),
        art_type=str(getattr(art, "type", "") or ""),
        max_chars=max_chars,
    )


def _chunks_already_llm_summarized(chunks: list[str]) -> bool:
    return bool(chunks) and all("[llm summary" in str(c) for c in chunks if str(c).strip())


def rebuild_prompt_excerpt_for_budget(
    art: Any,
    budget_tokens: int,
    *,
    phase: str = "",
) -> tuple[str, list[str]]:
    """Rebuild excerpt from raw using prompt budget; final_answer may run LLM 1-pass."""
    if budget_tokens <= 0:
        return "", []
    max_chars = min(DEFAULT_INGEST_EXCERPT_CHARS, max(1500, budget_tokens * 3))
    if phase in ("final_answer", "partial_final_answer", "recovery_final"):
        max_chars = min(ARTIFACT_FINAL_EXCERPT_CHARS, max(4000, budget_tokens * 3))
    existing = str(getattr(art, "prompt_excerpt", "") or "").strip()
    existing_chunks = list(getattr(art, "excerpt_chunks", None) or [])
    if existing and "[llm summary" in existing and _estimate_tokens(existing) <= int(budget_tokens * 0.95):
        return existing, existing_chunks
    if existing_chunks and _chunks_already_llm_summarized(existing_chunks):
        merged = _merge_chunks(existing_chunks, max_chars=max_chars)
        if merged.strip() and _estimate_tokens(merged) <= int(budget_tokens * 0.95):
            return merged, existing_chunks
    raw = _load_raw_from_artifact(art)
    if not raw.strip():
        return "", []
    excerpt, chunks = build_prompt_excerpt(
        raw,
        path=str(getattr(art, "path", "") or ""),
        tool_name=str(getattr(art, "name", "") or ""),
        art_type=str(getattr(art, "type", "") or ""),
        max_chars=max_chars,
    )
    if phase in ("final_answer", "partial_final_answer", "recovery_final") and _llm_summarize_enabled():
        if not _chunks_already_llm_summarized(chunks):
            chunks = apply_llm_one_pass(
                chunks,
                path=str(getattr(art, "path", "") or ""),
                raw_len=len(raw),
                kind=str(getattr(art, "name", "") or "grep").lower(),
                min_chunk_chars=ARTIFACT_LLM_FINAL_MIN_CHUNK_CHARS,
                force=True,
            )
        excerpt = _merge_chunks(chunks, max_chars=max_chars)
    return excerpt, chunks


def artifact_prompt_text(art: Any, budget_tokens: int = 0, *, phase: str = "") -> str:
    """Text for LLM prompts — never uses art.summary or preview compact."""
    cache_key = _cache_key(art, budget_tokens, phase)
    if cache_key in _FINAL_EXCERPT_CACHE:
        return _FINAL_EXCERPT_CACHE[cache_key]

    final_phase = phase in ("final_answer", "partial_final_answer", "recovery_final")
    excerpt = str(getattr(art, "prompt_excerpt", "") or "").strip()
    excerpt_tokens = _estimate_tokens(excerpt)
    existing_chunks = list(getattr(art, "excerpt_chunks", None) or [])
    has_llm_digest = "[llm summary" in excerpt or _chunks_already_llm_summarized(existing_chunks)

    if budget_tokens > 0:
        target = int(budget_tokens * 0.85)
        if excerpt and (has_llm_digest or excerpt_tokens >= target):
            result = truncate_to_token_budget(excerpt, budget_tokens)
            _FINAL_EXCERPT_CACHE[cache_key] = result
            return result
        if existing_chunks and _chunks_already_llm_summarized(existing_chunks):
            merged = _merge_chunks(existing_chunks, max_chars=budget_tokens * 3)
            if merged.strip() and _estimate_tokens(merged) >= min(target, 200):
                result = truncate_to_token_budget(merged, budget_tokens)
                _FINAL_EXCERPT_CACHE[cache_key] = result
                return result
        raw = _load_raw_from_artifact(art)
        raw_len = len(raw)
        should_rebuild = not excerpt
        if not should_rebuild and excerpt_tokens < target and raw_len > max(len(excerpt) * 2, 1200):
            should_rebuild = not has_llm_digest
        if should_rebuild and raw.strip():
            rebuilt, _ = rebuild_prompt_excerpt_for_budget(art, budget_tokens, phase=phase)
            if rebuilt.strip():
                excerpt = rebuilt
                excerpt_tokens = _estimate_tokens(excerpt)

    if not excerpt:
        excerpt, _ = rebuild_prompt_excerpt_from_artifact(art)
    if not excerpt:
        _FINAL_EXCERPT_CACHE[cache_key] = ""
        return ""
    result = truncate_to_token_budget(excerpt, budget_tokens) if budget_tokens > 0 else excerpt
    if final_phase and result.strip():
        _FINAL_EXCERPT_CACHE[cache_key] = result
    return result


def artifact_prompt_tokens(art: Any, budget_tokens: int = 0, *, phase: str = "") -> int:
    return _estimate_tokens(artifact_prompt_text(art, budget_tokens, phase=phase))
