"""Memory size caps and prune policy for Journal / EvidenceAnchor / Handoff."""

from __future__ import annotations

import json
import os
from typing import Any

MAX_JOURNAL_EVENTS = int(os.getenv("MAX_JOURNAL_EVENTS", "200"))
MAX_ANCHORS_TOTAL = int(os.getenv("MAX_ANCHORS_TOTAL", "300"))
MAX_ANCHORS_PER_FILE = int(os.getenv("MAX_ANCHORS_PER_FILE", "12"))
MAX_ANCHOR_SUMMARY_CHARS = int(os.getenv("MAX_ANCHOR_SUMMARY_CHARS", "800"))
MAX_HANDOFF_CHARS = int(os.getenv("MAX_HANDOFF_CHARS", "16000"))
MAX_HANDOFF_JOURNAL_TAIL = int(os.getenv("MAX_HANDOFF_JOURNAL_TAIL", "20"))


def truncate_summary(text: str) -> str:
    t = (text or "").strip()
    if len(t) <= MAX_ANCHOR_SUMMARY_CHARS:
        return t
    return t[: MAX_ANCHOR_SUMMARY_CHARS - 3] + "..."


def prune_journal(journal: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(journal) <= MAX_JOURNAL_EVENTS:
        return journal
    return list(journal[-MAX_JOURNAL_EVENTS:])


def prune_anchors(anchors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not anchors:
        return []
    trimmed = list(anchors)
    if len(trimmed) > MAX_ANCHORS_TOTAL:
        trimmed = trimmed[-MAX_ANCHORS_TOTAL:]
    counts: dict[str, int] = {}
    kept_rev: list[dict[str, Any]] = []
    for a in reversed(trimmed):
        if not isinstance(a, dict):
            continue
        path = str(a.get("path") or "").lower() or "__unknown__"
        if counts.get(path, 0) >= MAX_ANCHORS_PER_FILE:
            continue
        counts[path] = counts.get(path, 0) + 1
        kept_rev.append(a)
    return list(reversed(kept_rev))


def cap_handoff_dict(handoff: dict[str, Any]) -> dict[str, Any]:
    """Trim list fields and enforce serialized size budget."""
    ho = dict(handoff)
    for key, limit in (
        ("files_read", 20),
        ("commands_run", 10),
        ("evidence_collected", 12),
        ("coverage_targets", 12),
        ("remaining_risks", 8),
    ):
        if key in ho and isinstance(ho[key], list):
            ho[key] = list(ho[key])[-limit:]

    tail = list(ho.get("journal_tail") or [])
    if tail:
        ho["journal_tail"] = tail[-MAX_HANDOFF_JOURNAL_TAIL:]

    fa = ho.get("failed_actions")
    if isinstance(fa, dict) and len(fa) > 8:
        ho["failed_actions"] = dict(list(fa.items())[-8:])

    while len(json.dumps(ho, ensure_ascii=False, default=str)) > MAX_HANDOFF_CHARS and ho.get("journal_tail"):
        ho["journal_tail"] = ho["journal_tail"][1:]
    return ho
