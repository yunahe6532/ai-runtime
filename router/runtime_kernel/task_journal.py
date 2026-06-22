"""Task Journal & Handoff Ledger — Runtime Memory for long-running work."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class JournalEntry:
    ts: str
    kind: str  # read | edit | tool | command | failure | success | note
    target: str = ""
    why: str = ""
    summary: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append_journal(state: Any, entry: JournalEntry | dict[str, Any]) -> None:
    journal = list(getattr(state, "task_journal", None) or [])
    if isinstance(entry, dict):
        entry = JournalEntry(
            ts=str(entry.get("ts") or _now()),
            kind=str(entry.get("kind") or "note"),
            target=str(entry.get("target") or ""),
            why=str(entry.get("why") or ""),
            summary=str(entry.get("summary") or ""),
            meta=dict(entry.get("meta") or {}),
        )
    journal.append(entry.to_dict())
    if len(journal) > 200:
        journal = journal[-200:]
    state.task_journal = journal


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
    }
    state.handoff = handoff
    return handoff


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
