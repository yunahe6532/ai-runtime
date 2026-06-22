"""Evidence clusters — dedupe Read/Grep/range/tool into reusable working-set units."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .evidence_keys import (
    ArtifactKey,
    artifact_key_from_artifact,
    artifact_key_from_target,
    artifact_key_from_tool_message,
    canonical_artifact_key,
    evidence_cluster_id,
    normalize_path,
    normalize_symbol,
)

SYMBOL_DEF_RE = re.compile(r"^\s*(?:def|class|function)\s+(\w+)", re.MULTILINE)


@dataclass
class EvidenceClusterRecord:
    cluster_id: str
    path: str = ""
    sources: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    tool_call_ids: list[str] = field(default_factory=list)
    satisfied: bool = False
    stale: bool = False
    access_count: int = 0
    canonical_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "path": self.path,
            "sources": list(self.sources),
            "artifact_ids": list(self.artifact_ids),
            "tool_call_ids": list(self.tool_call_ids),
            "satisfied": self.satisfied,
            "stale": bool(self.stale),
            "access_count": self.access_count,
            "canonical_keys": list(self.canonical_keys),
        }


def _clusters(state: Any) -> dict[str, dict[str, Any]]:
    raw = getattr(state, "evidence_clusters", None) or {}
    if isinstance(raw, dict):
        return raw
    return {}


def _stats(state: Any) -> dict[str, int]:
    raw = getattr(state, "read_avoidance_stats", None) or {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "attempts": int(raw.get("attempts", 0) or 0),
        "avoided": int(raw.get("avoided", 0) or 0),
        "redundant": int(raw.get("redundant", 0) or 0),
    }


def _save_stats(state: Any, stats: dict[str, int]) -> None:
    state.read_avoidance_stats = dict(stats)


def _get_cluster(state: Any, cluster_id: str) -> EvidenceClusterRecord:
    clusters = _clusters(state)
    data = clusters.get(cluster_id)
    if isinstance(data, dict):
        return EvidenceClusterRecord(
            cluster_id=cluster_id,
            path=str(data.get("path") or ""),
            sources=list(data.get("sources") or []),
            artifact_ids=list(data.get("artifact_ids") or []),
            tool_call_ids=list(data.get("tool_call_ids") or []),
            satisfied=bool(data.get("satisfied")),
            stale=bool(data.get("stale")),
            access_count=int(data.get("access_count") or 0),
            canonical_keys=list(data.get("canonical_keys") or []),
        )
    return EvidenceClusterRecord(cluster_id=cluster_id)


def _put_cluster(state: Any, rec: EvidenceClusterRecord) -> None:
    clusters = _clusters(state)
    clusters[rec.cluster_id] = rec.to_dict()
    state.evidence_clusters = clusters


def invalidate_cluster(state: Any, cluster_id: str, *, reason: str = "stale") -> None:
    rec = _get_cluster(state, cluster_id)
    if rec.cluster_id not in _clusters(state):
        return
    rec.stale = True
    rec.satisfied = False
    rec.sources.append(f"invalidated:{reason}")
    _put_cluster(state, rec)


def record_avoided_full_read(state: Any, path: str, *, workspace: str = "", reason: str = "cluster_reuse") -> None:
    """Count a full Read attempt that was skipped in favour of cluster reuse."""
    stats = _stats(state)
    stats["attempts"] += 1
    stats["avoided"] += 1
    stats["redundant"] += 1
    _save_stats(state, stats)
    cid = evidence_cluster_id(ArtifactKey(path=path, kind="file"), workspace)
    rec = _get_cluster(state, cid)
    if reason not in rec.sources:
        rec.sources.append(reason)
    _put_cluster(state, rec)


def record_evidence_access(
    state: Any,
    key: ArtifactKey,
    *,
    source: str,
    artifact_id: str = "",
    tool_call_id: str = "",
    workspace: str = "",
) -> tuple[str, bool]:
    """Record access; return (cluster_id, redundant)."""
    stats = _stats(state)
    stats["attempts"] += 1
    cid = evidence_cluster_id(key, workspace)
    canon = canonical_artifact_key(key, workspace)
    rec = _get_cluster(state, cid)
    rec.path = rec.path or normalize_path(key.path, workspace)
    if tool_call_id and tool_call_id not in rec.tool_call_ids:
        rec.tool_call_ids.append(tool_call_id)
    if artifact_id and artifact_id not in rec.artifact_ids:
        rec.artifact_ids.append(artifact_id)
    if source not in rec.sources:
        rec.sources.append(source)
    if canon not in rec.canonical_keys:
        rec.canonical_keys.append(canon)

    redundant = False
    if rec.stale:
        stats["avoided"] += 1
        rec.stale = False
    if rec.satisfied and not rec.stale:
        if key.kind == "file" and "read" in rec.sources:
            redundant = True
        elif canon in rec.canonical_keys and source in rec.sources:
            redundant = True

    if redundant:
        stats["avoided"] += 1
        stats["redundant"] += 1
    else:
        rec.access_count += 1
        if key.kind in ("file", "grep", "symbol", "range", "tool_result"):
            rec.satisfied = True
            rec.stale = False

    _put_cluster(state, rec)
    _save_stats(state, stats)
    return cid, redundant


def record_artifact_access(state: Any, art: Any, *, workspace: str = "") -> tuple[str, bool]:
    key = artifact_key_from_artifact(art, workspace)
    source = "read" if getattr(art, "type", "") == "file_read" else "tool_result"
    if str(getattr(art, "name", "") or "").lower().find("grep") >= 0:
        source = "grep"
    return record_evidence_access(
        state,
        key,
        source=source,
        artifact_id=str(getattr(art, "artifact_id", "") or ""),
        workspace=workspace,
    )


def record_tool_message_access(state: Any, msg: dict[str, Any], *, workspace: str = "") -> tuple[str, bool]:
    key = artifact_key_from_tool_message(msg, workspace)
    source = "grep" if key.kind == "grep" else "tool_result"
    return record_evidence_access(
        state,
        key,
        source=source,
        tool_call_id=key.tool_call_id,
        workspace=workspace,
    )


def should_skip_full_read(state: Any, path: str, *, workspace: str = "") -> tuple[bool, str]:
    """Recovery dedup — block redundant full Read when cluster already satisfied."""
    key = ArtifactKey(path=path, kind="file")
    cid = evidence_cluster_id(key, workspace)
    rec = _get_cluster(state, cid)
    if rec.satisfied and not rec.stale and "read" in rec.sources and rec.artifact_ids:
        return True, "cluster_satisfied"
    return False, ""


def extract_symbol_slice(text: str, symbol: str) -> str:
    sym = normalize_symbol(symbol)
    if not sym or not text:
        return ""
    lines = text.splitlines()
    buf: list[str] = []
    capture = False
    indent = ""
    for line in lines:
        if re.match(rf"^\s*(?:def|class)\s+{re.escape(sym)}\b", line):
            capture = True
            indent = line[: len(line) - len(line.lstrip())]
            buf = [line]
            continue
        if capture:
            if line.strip() and not line.startswith(indent) and not line.startswith(indent + " "):
                if line[0] not in (" ", "\t"):
                    break
            buf.append(line)
    if buf:
        return "\n".join(buf)
    if sym in text:
        return sym
    return ""


def recovery_retrieval_hints(
    state: Any,
    context_need: Any,
    coverage: Any,
    *,
    workspace: str = "",
) -> dict[str, Any]:
    """Prefer symbol/range slices; skip full re-read of satisfied files."""
    skip_paths: list[str] = []
    prefer_symbols: list[str] = []
    reuse_artifact_ids: list[str] = []

    for target in getattr(context_need, "coverage_targets", None) or []:
        key = artifact_key_from_target(str(target))
        cid = evidence_cluster_id(key, workspace)
        rec = _get_cluster(state, cid)
        if key.symbol:
            prefer_symbols.append(normalize_symbol(key.symbol))
        if key.path:
            skip, _ = should_skip_full_read(state, key.path, workspace=workspace)
            if skip:
                skip_paths.append(normalize_path(key.path, workspace))
                reuse_artifact_ids.extend(rec.artifact_ids)

    for miss in getattr(coverage, "symbol_missing", None) or []:
        key = artifact_key_from_target(str(miss))
        if key.symbol:
            prefer_symbols.append(normalize_symbol(key.symbol))

    for _cid, data in _clusters(state).items():
        if isinstance(data, dict) and data.get("satisfied") and data.get("artifact_ids"):
            reuse_artifact_ids.extend(list(data.get("artifact_ids") or []))

    return {
        "skip_full_read_paths": list(dict.fromkeys(skip_paths)),
        "prefer_symbols": list(dict.fromkeys(prefer_symbols)),
        "reuse_artifact_ids": list(dict.fromkeys(reuse_artifact_ids)),
    }


def cluster_avoidance_rate(state: Any) -> float:
    stats = _stats(state)
    if stats["attempts"] > 0:
        return round(stats["avoided"] / stats["attempts"], 4)
    clusters = _clusters(state)
    total = 0
    redundant = 0
    for data in clusters.values():
        if not isinstance(data, dict):
            continue
        ac = int(data.get("access_count") or 0)
        total += ac
        if data.get("satisfied") and ac > 1:
            redundant += ac - 1
    if total <= 0:
        return 1.0
    return round(max(0.0, 1.0 - redundant / total), 4)
