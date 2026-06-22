"""Canonical artifact keys and evidence cluster IDs — pure, no I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SYMBOL_TARGET_RE = re.compile(r"^([^:]+)::(.+)$")
RANGE_TARGET_RE = re.compile(r"^([^:#]+):(\d+)(?:-(\d+))?$")


@dataclass(frozen=True)
class ArtifactKey:
    path: str = ""
    symbol: str = ""
    range_start: int | None = None
    range_end: int | None = None
    tool_call_id: str = ""
    kind: str = "file"  # file | symbol | range | tool_result | grep


def normalize_path(path: str, workspace: str = "") -> str:
    """Basename-normalized path for cross-tier matching."""
    p = (path or "").strip().replace("\\", "/")
    if not p:
        return ""
    ws = (workspace or "").strip().replace("\\", "/").rstrip("/")
    if ws and p.startswith(ws):
        p = p[len(ws) :].lstrip("/")
    while p.startswith("./"):
        p = p[2:]
    if "/" in p:
        p = p.split("/")[-1]
    return p.lower()


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().lower()


def normalize_range(start: int | None, end: int | None) -> tuple[int | None, int | None]:
    if start is None:
        return None, None
    e = end if end is not None else start
    if e < start:
        start, e = e, start
    return start, e


def canonical_artifact_key(key: ArtifactKey, workspace: str = "") -> str:
    """Stable key: file.py · file.py::func · file.py:10-40 · tool:<id>."""
    path = normalize_path(key.path, workspace)
    sym = normalize_symbol(key.symbol)
    rs, re_ = normalize_range(key.range_start, key.range_end)
    if sym and path:
        return f"{path}::{sym}"
    if path and rs is not None:
        return f"{path}:{rs}-{re_}"
    if path:
        return path
    if key.tool_call_id:
        return f"tool:{key.tool_call_id}"
    return "tool:latest"


def evidence_cluster_id(key: ArtifactKey, workspace: str = "") -> str:
    """Cluster Read/Grep/range/tool on the same file."""
    path = normalize_path(key.path, workspace)
    if path:
        return f"cluster:{path}"
    if key.tool_call_id:
        return f"cluster:tool:{key.tool_call_id}"
    return "cluster:tool:latest"


def artifact_key_from_target(target: str) -> ArtifactKey:
    t = (target or "").strip()
    m = SYMBOL_TARGET_RE.match(t)
    if m:
        return ArtifactKey(path=m.group(1), symbol=m.group(2), kind="symbol")
    m = RANGE_TARGET_RE.match(t)
    if m:
        end = int(m.group(3)) if m.group(3) else int(m.group(2))
        return ArtifactKey(path=m.group(1), range_start=int(m.group(2)), range_end=end, kind="range")
    if t:
        return ArtifactKey(path=t, kind="file")
    return ArtifactKey(kind="tool_result")


def artifact_key_from_artifact(art: Any, workspace: str = "") -> ArtifactKey:
    path = str(getattr(art, "path", "") or getattr(art, "name", "") or "")
    tool_call_id = str(getattr(art, "tool_call_id", "") or "")
    art_type = str(getattr(art, "type", "") or "")
    analysis = getattr(art, "analysis", None) or {}
    symbol = ""
    if isinstance(analysis, dict):
        symbol = str(analysis.get("symbol") or analysis.get("primary_symbol") or "")
    kind = "file"
    if art_type in ("tool_result", "shell_result"):
        kind = "tool_result"
        name = str(getattr(art, "name", "") or "").lower()
        if "grep" in name:
            kind = "grep"
    elif symbol:
        kind = "symbol"
    return ArtifactKey(path=path, symbol=symbol, tool_call_id=tool_call_id, kind=kind)


def artifact_key_from_tool_message(msg: dict[str, Any], workspace: str = "") -> ArtifactKey:
    name = str(msg.get("name") or "").lower()
    tool_call_id = str(msg.get("tool_call_id") or "")
    content = str(msg.get("content") or "")
    path = ""
    symbol = ""
    for line in content.splitlines()[:8]:
        if ".py" in line or ".md" in line:
            parts = line.split(":")
            if parts:
                path = parts[0].strip().split()[-1]
        if "::" in line:
            sym_m = SYMBOL_TARGET_RE.search(line)
            if sym_m:
                path = path or sym_m.group(1)
                symbol = sym_m.group(2)
    kind = "grep" if "grep" in name else "tool_result"
    return ArtifactKey(path=path, symbol=symbol, tool_call_id=tool_call_id, kind=kind)
