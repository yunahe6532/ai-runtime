"""Structured evidence items — raw trace refs + summaries for judge input."""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class EvidenceItem:
    id: str = ""
    tool: str = ""
    path: str = ""
    query: str = ""
    raw_ref: str = ""
    summary: str = ""
    evidence_type: str = ""
    confidence: float = 0.0
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EvidenceItem:
        if not data:
            return cls()
        fields = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**fields)


def _summarize_result(text: str, max_len: int = 240) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return (text or "")[:max_len]
    return " | ".join(lines[:3])[:max_len]


def build_evidence_item(
    *,
    tool: str,
    path: str = "",
    query: str = "",
    result_text: str = "",
    tags: list[str] | None = None,
    raw_ref: str = "",
) -> EvidenceItem:
    tags = tags or []
    ev_type = tags[0].split(":", 1)[0] if tags else "artifact_seen"
    sig = f"{tool}|{path}|{query}|{ev_type}|{len(result_text)}"
    eid = hashlib.sha256(sig.encode()).hexdigest()[:12]
    return EvidenceItem(
        id=eid,
        tool=tool,
        path=path,
        query=query,
        raw_ref=raw_ref,
        summary=_summarize_result(result_text),
        evidence_type=ev_type,
        confidence=0.85 if tags else 0.5,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


def append_evidence_item(state: Any, item: EvidenceItem, *, max_items: int = 40) -> None:
    raw_items: list[Any] = list(getattr(state, "evidence_items", None) or [])
    ids = {
        (i.get("id") if isinstance(i, dict) else i.id)
        for i in raw_items
    }
    if item.id in ids:
        return
    raw_items.append(item.to_dict())
    state.evidence_items = raw_items[-max_items:]


def evidence_items_for_judge(state: Any, limit: int = 12) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for raw in list(getattr(state, "evidence_items", None) or [])[-limit:]:
        d = raw if isinstance(raw, dict) else raw.to_dict()
        out.append({
            "type": str(d.get("evidence_type") or ""),
            "tool": str(d.get("tool") or ""),
            "path": str(d.get("path") or ""),
            "query": str(d.get("query") or ""),
            "summary": str(d.get("summary") or "")[:200],
        })
    return out
