"""EvidenceAnchor ingest — wire Read/Grep/Shell/Edit tool results → anchors + journal."""

from __future__ import annotations

import logging
import re
from typing import Any

from .evidence_anchor import EvidenceAnchor, content_hash, upsert_anchor
from .task_journal import (
    JournalKind,
    record_edit,
    record_failure,
    record_grep,
    record_read,
    record_shell,
    record_success,
    record_tool,
)

LOG = logging.getLogger("runtime_kernel.evidence_ingest")

LINE_NUMBERED_RE = re.compile(r"^\s*(\d+)\|", re.M)
SYMBOL_DEF_RE = re.compile(r"^\s*(?:def|class|async def)\s+(\w+)", re.M)
GREP_WORKSPACE_RE = re.compile(r"<workspace_result[^>]*>(.*?)</workspace_result>", re.S | re.I)
EDIT_TOOL_NAMES = frozenset({"write", "strreplace", "edit", "applypatch", "edit_file"})


def _line_range_from_text(text: str) -> tuple[int, int]:
    nums: list[int] = []
    for m in LINE_NUMBERED_RE.finditer(text or ""):
        try:
            nums.append(int(m.group(1)))
        except ValueError:
            continue
    if not nums:
        return 0, 0
    return min(nums), max(nums)


def _symbol_from_text(text: str) -> str:
    m = SYMBOL_DEF_RE.search(text or "")
    return m.group(1) if m else ""


def _resolve_tool_args(
    art: Any,
    delta: Any | None,
    messages: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if art.path:
        args["path"] = art.path
    if art.command:
        args["command"] = art.command
    tcid = str(getattr(art, "tool_call_id", "") or "")
    if not tcid or not delta or not messages:
        return args
    try:
        from legacy.memory_store import _resolve_tool_call_args

        tc_args = _resolve_tool_call_args({"tool_call_id": tcid}, delta, messages=messages)
        args.update({k: v for k, v in tc_args.items() if v})
    except Exception:
        pass
    return args


def anchor_from_artifact(
    art: Any,
    *,
    raw_text: str,
    tool_args: dict[str, Any] | None = None,
    query: str = "",
) -> EvidenceAnchor | None:
    """Build EvidenceAnchor from a stored artifact."""
    tool = (art.name or "").strip()
    tool_l = tool.lower()
    path = str(art.path or "")
    if tool_args:
        path = path or str(tool_args.get("path") or tool_args.get("target_directory") or "")

    text = raw_text or ""
    line_start, line_end = _line_range_from_text(text)
    symbol = _symbol_from_text(text)
    summary = (art.prompt_excerpt or art.summary or "")[:800]
    why = f"tool={tool or art.type}"

    if art.type == "file_read" or tool_l in ("read", "readsource"):
        if not path and art.index_terms:
            path = art.index_terms[0]
        return EvidenceAnchor(
            path=path or "unknown",
            symbol=symbol,
            line_start=line_start,
            line_end=line_end,
            content_hash=content_hash(text),
            summary=summary,
            why_read=why,
            evidence_quality=0.9 if line_start else 0.7,
            last_used_task=query[:120],
            artifact_id=art.artifact_id,
            meta={"art_type": art.type, "chars": art.chars},
        )

    if tool_l in ("grep", "grepsource") or "<workspace_result" in text[:1200]:
        pattern = str((tool_args or {}).get("pattern") or "")
        grep_paths = re.findall(r"^([\w./~-]+\.(?:py|md|ts|tsx|js|yaml|yml|sh))", text, re.M | re.I)
        anchor_path = grep_paths[0] if grep_paths else (path or f"grep:{pattern[:40]}")
        return EvidenceAnchor(
            path=anchor_path,
            symbol=pattern[:80],
            line_start=0,
            line_end=0,
            content_hash=content_hash(text[:4000]),
            summary=summary or text[:400],
            why_read=f"grep pattern={pattern or '.'}",
            evidence_quality=0.75,
            last_used_task=query[:120],
            artifact_id=art.artifact_id,
            meta={"pattern": pattern, "match_count": len(grep_paths)},
        )

    if tool_l in ("glob", "globsource"):
        pattern = str((tool_args or {}).get("glob_pattern") or (tool_args or {}).get("pattern") or "*.py")
        dir_path = str((tool_args or {}).get("target_directory") or path or ".")
        return EvidenceAnchor(
            path=dir_path,
            symbol=pattern,
            content_hash=content_hash(text[:2000]),
            summary=summary or text[:300],
            why_read="glob inventory",
            evidence_quality=0.6,
            last_used_task=query[:120],
            artifact_id=art.artifact_id,
            meta={"glob_pattern": pattern},
        )

    if art.type == "shell_result" or tool_l == "shell":
        cmd = art.command or str((tool_args or {}).get("command") or "")
        exit_ok = "Exit code: 0" in text
        return EvidenceAnchor(
            path=path or "shell",
            symbol=cmd[:80],
            content_hash=content_hash(text[:4000]),
            summary=summary or text[:400],
            why_read="shell execution",
            evidence_quality=0.85 if exit_ok else 0.4,
            last_used_task=query[:120],
            artifact_id=art.artifact_id,
            meta={"exit_ok": exit_ok, "command": cmd[:200]},
        )

    if tool_l in EDIT_TOOL_NAMES:
        return EvidenceAnchor(
            path=path or "edit",
            symbol="edit",
            content_hash=content_hash(text[:2000]),
            summary=summary or "file modified",
            why_read="code edit",
            evidence_quality=0.8,
            last_used_task=query[:120],
            artifact_id=art.artifact_id,
            meta={"tool": tool},
        )

    if art.type in ("tool_result", "tool_call") and text.strip():
        return EvidenceAnchor(
            path=path or tool or art.type,
            content_hash=content_hash(text[:2000]),
            summary=summary or text[:300],
            why_read=why,
            evidence_quality=0.5,
            last_used_task=query[:120],
            artifact_id=art.artifact_id,
        )

    return None


def _journal_from_artifact(
    state: Any,
    art: Any,
    *,
    raw_text: str,
    tool_args: dict[str, Any],
    query: str,
    success: bool,
) -> None:
    tool = (art.name or "").strip()
    tool_l = tool.lower()
    path = art.path or str(tool_args.get("path") or "")

    if not success:
        record_failure(
            state,
            target=path or tool or art.artifact_id,
            why=tool or art.type,
            summary=(raw_text or art.summary or "")[:200],
        )
        return

    if art.type == "file_read" or tool_l in ("read", "readsource"):
        ls, le = _line_range_from_text(raw_text)
        record_read(
            state,
            path=path or "unknown",
            why=query[:120],
            summary=(art.prompt_excerpt or art.summary or "")[:300],
            line_start=ls,
            line_end=le,
            artifact_id=art.artifact_id,
        )
        return

    if tool_l in ("grep", "grepsource"):
        record_grep(
            state,
            pattern=str(tool_args.get("pattern") or "."),
            path=path,
            summary=(art.summary or raw_text[:200]),
            artifact_id=art.artifact_id,
        )
        return

    if tool_l in EDIT_TOOL_NAMES:
        record_edit(
            state,
            path=path or "unknown",
            why=query[:120],
            summary=(art.summary or "edit applied")[:200],
            artifact_id=art.artifact_id,
        )
        return

    if art.type == "shell_result" or tool_l == "shell":
        cmd = art.command or str(tool_args.get("command") or "")
        exit_ok = "Exit code: 0" in raw_text
        record_shell(
            state,
            command=cmd[:200],
            summary=(art.summary or raw_text[:200]),
            success=exit_ok,
            artifact_id=art.artifact_id,
        )
        if exit_ok:
            record_success(state, target=cmd[:80], summary="shell exit 0")
        return

    record_tool(
        state,
        tool=tool or art.type,
        target=path or art.artifact_id,
        summary=(art.summary or raw_text[:200]),
        meta={"artifact_id": art.artifact_id},
    )


def ingest_artifacts_evidence(
    state: Any,
    artifacts: list[Any],
    *,
    delta: Any | None = None,
    messages: list[dict[str, Any]] | None = None,
    query: str = "",
) -> int:
    """Upsert EvidenceAnchor + normalized journal for each artifact. Returns anchor count."""
    if not state or not artifacts:
        return 0
    try:
        from legacy.memory_store import _artifact_raw_text, _tool_success_from_text
    except ImportError:
        return 0

    count = 0
    for art in artifacts:
        if art.type not in ("file_read", "shell_result", "tool_result", "tool_call"):
            continue
        raw_text = _artifact_raw_text(art)
        success = _tool_success_from_text(art, raw_text)
        tool_args = _resolve_tool_args(art, delta, messages)

        anchor = anchor_from_artifact(art, raw_text=raw_text, tool_args=tool_args, query=query)
        if anchor:
            upsert_anchor(state, anchor)
            count += 1

        _journal_from_artifact(
            state, art, raw_text=raw_text, tool_args=tool_args, query=query, success=success,
        )

    if count:
        LOG.info("evidence_ingest anchors=%d journal_len=%d", count, len(getattr(state, "task_journal", None) or []))
    try:
        from explorer_trace import write_explorer_trace

        turn_index = int(getattr(state, "turn_index", 0) or 0)
        for art in artifacts:
            if art.type not in ("file_read", "shell_result", "tool_result", "tool_call"):
                continue
            write_explorer_trace(
                "memory.evidence.upserted",
                phase=getattr(state, "phase_hint", "") or "",
                query=query[:500],
                turn_index=turn_index,
                path=art.path or art.name or art.type,
                tool_name=art.name or art.type,
                result_summary=(art.summary or art.prompt_excerpt or "")[:300],
            )
    except Exception:
        pass
    return count
