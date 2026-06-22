"""Evidence anchors — summary + line range + content hash (not summary-only)."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class EvidenceAnchor:
    path: str
    symbol: str = ""
    line_start: int = 0
    line_end: int = 0
    content_hash: str = ""
    summary: str = ""
    why_read: str = ""
    evidence_quality: float = 1.0
    last_used_task: str = ""
    artifact_id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EvidenceAnchor | None:
        if not data or not data.get("path"):
            return None
        fields = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**fields)

    @property
    def anchor_key(self) -> str:
        base = f"{self.path}:{self.symbol}:{self.line_start}-{self.line_end}"
        return hashlib.sha256(base.encode()).hexdigest()[:16]


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()[:16]


def upsert_anchor(state: Any, anchor: EvidenceAnchor | dict[str, Any]) -> None:
    anchors = list(getattr(state, "evidence_anchors", None) or [])
    a = anchor if isinstance(anchor, EvidenceAnchor) else EvidenceAnchor.from_dict(anchor)
    if not a:
        return
    key = a.anchor_key
    kept = [x for x in anchors if isinstance(x, dict) and x.get("anchor_key") != key]
    d = a.to_dict()
    d["anchor_key"] = key
    kept.append(d)
    if len(kept) > 300:
        kept = kept[-300:]
    state.evidence_anchors = kept


def anchors_for_path(state: Any, path: str) -> list[dict[str, Any]]:
    p = (path or "").lower()
    return [
        x for x in (getattr(state, "evidence_anchors", None) or [])
        if isinstance(x, dict) and p in str(x.get("path", "")).lower()
    ]
