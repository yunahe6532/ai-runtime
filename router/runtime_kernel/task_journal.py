"""Task Journal & Handoff Ledger — Runtime Memory for long-running work."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .memory_limits import cap_handoff_dict, prune_journal


class JournalKind(str, Enum):
    READ = "read"
    GREP = "grep"
    GLOB = "glob"
    EDIT = "edit"
    TOOL = "tool"
    SHELL = "shell"
    COMMAND = "command"
    FAILURE = "failure"
    SUCCESS = "success"
    NOTE = "note"
    TURN = "turn"


@dataclass
class JournalEntry:
    ts: str
    kind: str  # read | grep | glob | edit | tool | shell | command | failure | success | note | turn
    target: str = ""
    why: str = ""
    summary: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _normalize_kind(kind: str) -> str:
    raw = (kind or "note").strip().lower()
    for jk in JournalKind:
        if raw == jk.value:
            return jk.value
    return raw if raw in {jk.value for jk in JournalKind} else JournalKind.NOTE.value


def append_journal(state: Any, entry: JournalEntry | dict[str, Any]) -> None:
    journal = list(getattr(state, "task_journal", None) or [])
    if isinstance(entry, dict):
        entry = JournalEntry(
            ts=str(entry.get("ts") or _now()),
            kind=_normalize_kind(str(entry.get("kind") or "note")),
            target=str(entry.get("target") or ""),
            why=str(entry.get("why") or ""),
            summary=str(entry.get("summary") or ""),
            meta=dict(entry.get("meta") or {}),
        )
    else:
        entry = JournalEntry(
            ts=entry.ts or _now(),
            kind=_normalize_kind(entry.kind),
            target=entry.target,
            why=entry.why,
            summary=entry.summary,
            meta=dict(entry.meta or {}),
        )
    journal.append(entry.to_dict())
    state.task_journal = prune_journal(journal)
    try:
        from explorer_trace import write_explorer_trace

        write_explorer_trace(
            "memory.journal.appended",
            phase=getattr(state, "phase_hint", "") or "",
            query=str(getattr(state, "current_query", "") or "")[:500],
            turn_index=int(getattr(state, "turn_index", 0) or 0),
            target=entry.target,
            tool_name=entry.kind,
            kind=entry.kind,
            result_summary=entry.summary[:300],
            reason=entry.why[:200],
        )
    except Exception:
        pass


def record_read(
    state: Any,
    *,
    path: str,
    why: str = "",
    summary: str = "",
    line_start: int = 0,
    line_end: int = 0,
    artifact_id: str = "",
) -> None:
    meta: dict[str, Any] = {}
    if line_start:
        meta["line_start"] = line_start
        meta["line_end"] = line_end or line_start
    if artifact_id:
        meta["artifact_id"] = artifact_id
    append_journal(
        state,
        JournalEntry(
            ts=_now(),
            kind=JournalKind.READ.value,
            target=path,
            why=why,
            summary=summary,
            meta=meta,
        ),
    )


def record_grep(
    state: Any,
    *,
    pattern: str,
    path: str = "",
    summary: str = "",
    artifact_id: str = "",
) -> None:
    meta: dict[str, Any] = {"pattern": pattern}
    if artifact_id:
        meta["artifact_id"] = artifact_id
    append_journal(
        state,
        JournalEntry(
            ts=_now(),
            kind=JournalKind.GREP.value,
            target=path or pattern,
            why=f"grep:{pattern[:80]}",
            summary=summary,
            meta=meta,
        ),
    )


def record_edit(
    state: Any,
    *,
    path: str,
    why: str = "",
    summary: str = "",
    artifact_id: str = "",
) -> None:
    meta: dict[str, Any] = {}
    if artifact_id:
        meta["artifact_id"] = artifact_id
    append_journal(
        state,
        JournalEntry(
            ts=_now(),
            kind=JournalKind.EDIT.value,
            target=path,
            why=why,
            summary=summary,
            meta=meta,
        ),
    )


def record_shell(
    state: Any,
    *,
    command: str,
    summary: str = "",
    success: bool = True,
    artifact_id: str = "",
) -> None:
    meta: dict[str, Any] = {"success": success}
    if artifact_id:
        meta["artifact_id"] = artifact_id
    append_journal(
        state,
        JournalEntry(
            ts=_now(),
            kind=JournalKind.SHELL.value,
            target=command[:200],
            why="shell",
            summary=summary,
            meta=meta,
        ),
    )


def record_tool(
    state: Any,
    *,
    tool: str,
    target: str = "",
    summary: str = "",
    meta: dict[str, Any] | None = None,
) -> None:
    append_journal(
        state,
        JournalEntry(
            ts=_now(),
            kind=JournalKind.TOOL.value,
            target=target or tool,
            why=tool,
            summary=summary,
            meta=dict(meta or {}),
        ),
    )


def record_failure(
    state: Any,
    *,
    target: str,
    why: str = "",
    summary: str = "",
) -> None:
    append_journal(
        state,
        JournalEntry(
            ts=_now(),
            kind=JournalKind.FAILURE.value,
            target=target,
            why=why,
            summary=summary,
        ),
    )


def record_success(
    state: Any,
    *,
    target: str,
    summary: str = "",
) -> None:
    append_journal(
        state,
        JournalEntry(
            ts=_now(),
            kind=JournalKind.SUCCESS.value,
            target=target,
            summary=summary,
        ),
    )


def record_turn_journal(
    state: Any,
    *,
    query: str,
    phase: str,
    intent: str,
    files_read: list[str] | None = None,
    tool_summary: str = "",
) -> None:
    append_journal(
        state,
        JournalEntry(
            ts=_now(),
            kind="turn",
            target=query[:200],
            why=f"phase={phase} intent={intent}",
            summary=tool_summary[:500],
            meta={"files_read": list(files_read or [])[:8]},
        ),
    )


def build_handoff(state: Any, *, query: str = "") -> dict[str, Any]:
    """Handoff snapshot for next turn — render-first, LLM polish optional."""
    ap = getattr(state, "agent_plan", None) or {}
    journal = list(getattr(state, "task_journal", None) or [])[-20:]
    handoff = {
        "updated_at": _now(),
        "query": query or getattr(state, "current_query", ""),
        "phase_hint": getattr(state, "phase_hint", ""),
        "files_read": list(getattr(state, "files_read", None) or [])[-20:],
        "commands_run": list(getattr(state, "commands_run", None) or [])[-10:],
        "evidence_collected": list(ap.get("evidence_collected") or [])[-12:],
        "coverage_targets": list(ap.get("preferred_sources") or ap.get("required_source_ids") or [])[:12],
        "failed_actions": dict(getattr(state, "failed_actions", None) or {}),
        "journal_tail": journal,
        "remaining_risks": list(ap.get("missing_evidence") or getattr(state, "missing_evidence", None) or [])[:8],
        "anchor_count": len(getattr(state, "evidence_anchors", None) or []),
        "journal_count": len(getattr(state, "task_journal", None) or []),
    }
    state.handoff = cap_handoff_dict(handoff)
    return state.handoff


def render_handoff_markdown(handoff: dict[str, Any]) -> str:
    lines = ["[Task Handoff]", f"query: {handoff.get('query', '')[:300]}"]
    files = handoff.get("files_read") or []
    if files:
        lines.append("touched_files: " + ", ".join(str(f) for f in files[:12]))
    ev = handoff.get("evidence_collected") or []
    if ev:
        lines.append("evidence: " + ", ".join(str(e) for e in ev[:8]))
    risks = handoff.get("remaining_risks") or []
    if risks:
        lines.append("remaining_risks: " + ", ".join(str(r) for r in risks[:6]))
    for j in (handoff.get("journal_tail") or [])[-5:]:
        if isinstance(j, dict):
            lines.append(f"- [{j.get('kind', '?')}] {j.get('target', '')[:80]} — {j.get('summary', '')[:120]}")
    lines.append("[/Task Handoff]")
    return "\n".join(lines)
