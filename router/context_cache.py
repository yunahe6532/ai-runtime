"""Raw payload storage and lightweight context indexing."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from capture import _content_text
from runtime_core.indexing_helpers import (
    Section,
    _compress_rules,
    _compress_summary,
    _compress_system,
    _extract_user_query,
    _extract_workspace_files,
    _summarize_tool,
    classify_message,
)
from message_index import stable_message_key as _message_hash

LOG = logging.getLogger("router.cache")

_DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "tmp" / "context-cache"
CACHE_DIR = Path(os.getenv("CONTEXT_CACHE_DIR", str(_DEFAULT_CACHE)))
RAW_DIR = CACHE_DIR / "raw"
INDEX_DIR = CACHE_DIR / "index"
_lock = threading.Lock()
_seq = 0


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _next_req_id() -> str:
    global _seq
    with _lock:
        _seq += 1
        return f"{int(time.time())}_{_seq:04d}"


def extract_last_user_query(body: dict[str, Any]) -> str:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        text = _content_text(msg.get("content", ""))
        if "<user_query>" in text:
            return _extract_user_query(text)
        if text.strip():
            return text.strip()
    return ""


@dataclass
class ToolResultRef:
    name: str
    hash: str
    chars: int
    preview: str
    is_error: bool


@dataclass
class ContextIndex:
    req_id: str
    query: str
    raw_tokens: int
    message_count: int
    tool_count: int
    system_prompt_hash: str = ""
    user_rules_hash: str = ""
    workspace_path: str = ""
    recent_files: list[str] = field(default_factory=list)
    file_refs: list[str] = field(default_factory=list)
    tool_results: list[ToolResultRef] = field(default_factory=list)
    project_summary: str = ""
    rules_summary: str = ""
    has_tools: bool = False
    tool_names: list[str] = field(default_factory=list)
    noise_count: int = 0

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "system_prompt_hash": self.system_prompt_hash,
            "user_rules_hash": self.user_rules_hash,
            "workspace_path": self.workspace_path,
            "recent_files": self.recent_files,
            "file_refs": self.file_refs,
            "tool_results": [tr.__dict__ for tr in self.tool_results],
            "project_summary": self.project_summary[:1000],
            "rules_summary": self.rules_summary[:1000],
            "tool_names": self.tool_names,
            "noise_count": self.noise_count,
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any], req_id: str, **overrides: Any) -> ContextIndex:
        tool_results = [
            ToolResultRef(**tr) for tr in (data.get("tool_results") or []) if isinstance(tr, dict)
        ]
        idx = cls(
            req_id=req_id,
            query=str(data.get("query") or ""),
            raw_tokens=int(overrides.get("raw_tokens") or 0),
            message_count=int(overrides.get("message_count") or 0),
            tool_count=int(overrides.get("tool_count") or 0),
            has_tools=bool(overrides.get("has_tools")),
            tool_names=list(data.get("tool_names") or []),
            system_prompt_hash=str(data.get("system_prompt_hash") or ""),
            user_rules_hash=str(data.get("user_rules_hash") or ""),
            workspace_path=str(data.get("workspace_path") or ""),
            recent_files=list(data.get("recent_files") or []),
            file_refs=list(data.get("file_refs") or []),
            tool_results=tool_results,
            project_summary=str(data.get("project_summary") or ""),
            rules_summary=str(data.get("rules_summary") or ""),
            noise_count=int(data.get("noise_count") or 0),
        )
        if overrides.get("query"):
            idx.query = str(overrides["query"])
        return idx

    def to_intent_prompt(self) -> str:
        lines = [
            "User query:",
            self.query,
            "",
            "Available context index:",
            f"- project: cursor-local-llm router",
            f"- workspace: {self.workspace_path or '(unknown)'}",
            f"- raw_payload_tokens: {self.raw_tokens}",
            f"- messages: {self.message_count}",
            f"- tools: {self.tool_count} ({', '.join(self.tool_names[:8])})",
            f"- system_prompt_hash: {self.system_prompt_hash[:12] or 'none'}",
            f"- user_rules_hash: {self.user_rules_hash[:12] or 'none'}",
            "- recently viewed files:",
        ]
        for f in self.recent_files[:8]:
            lines.append(f"  - {f}")
        if not self.recent_files:
            lines.append("  - (none)")
        lines.append("- cached tool results:")
        for tr in self.tool_results[-10:]:
            lines.append(
                f"  - {tr.name} hash={tr.hash[:8]} chars={tr.chars} error={tr.is_error} preview={tr.preview[:80]!r}"
            )
        if not self.tool_results:
            lines.append("  - (none)")
        if self.project_summary:
            lines.append("")
            lines.append("Project state summary (not full prior assistant):")
            lines.append(self.project_summary[:600])
        return "\n".join(lines)


def save_raw_payload(body: dict[str, Any]) -> str:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    req_id = _next_req_id()
    path = RAW_DIR / f"{req_id}.json"
    payload = {
        "id": req_id,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "body": body,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return req_id


def _apply_message_to_index(idx: ContextIndex, msg: dict[str, Any], i: int) -> None:
    from message_index import classify_message_kind, is_noise_kind

    kind = classify_message_kind(msg)
    if is_noise_kind(kind):
        idx.noise_count += 1
        return

    section = classify_message(msg, i)
    text = _content_text(msg.get("content", ""))
    h = _message_hash(msg)

    if section == Section.SYSTEM and not idx.system_prompt_hash:
        idx.system_prompt_hash = h
    elif section in (Section.USER_RULES, Section.USER_INFO) and "<user_rules>" in text:
        idx.user_rules_hash = h
        idx.rules_summary = _compress_rules(text)
    elif section == Section.WORKSPACE_STATE:
        idx.recent_files.extend(_extract_workspace_files(text))
    elif section == Section.SUMMARY and not idx.project_summary:
        idx.project_summary = _compress_summary(text)
    elif section == Section.TOOL:
        name = str(msg.get("name", "tool"))
        preview = text[:120].replace("\n", " ")
        idx.tool_results.append(
            ToolResultRef(
                name=name,
                hash=h,
                chars=len(text),
                preview=preview,
                is_error=text.strip().startswith("Error:") or "Traceback" in text[:300],
            )
        )
        idx.file_refs.extend(re.findall(r"([\w./~-]+\.(?:py|sh|yml|yaml|json|md))", text, re.I))

    if kind == "user_task" and "<user_query>" in text:
        idx.query = _extract_user_query(text) or text.strip()[:400]

    m = re.search(r"Workspace Path:\s*(\S+)", text)
    if m:
        idx.workspace_path = m.group(1)


def _rebuild_context_index(
    messages: list[dict[str, Any]],
    req_id: str,
    query: str,
    raw_tokens: int,
    tool_names: list[str],
) -> ContextIndex:
    idx = ContextIndex(
        req_id=req_id,
        query=query,
        raw_tokens=raw_tokens,
        message_count=len(messages),
        tool_count=len(tool_names),
        has_tools=bool(tool_names),
        tool_names=tool_names,
    )
    for i, msg in enumerate(messages):
        if isinstance(msg, dict):
            _apply_message_to_index(idx, msg, i)
    idx.recent_files = list(dict.fromkeys(idx.recent_files))[:12]
    idx.file_refs = list(dict.fromkeys(idx.file_refs))[:20]
    idx.tool_results = idx.tool_results[-30:]
    return idx


def build_context_index(
    body: dict[str, Any],
    req_id: str,
    *,
    state: Any | None = None,
    delta: Any | None = None,
) -> ContextIndex:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    query = extract_last_user_query(body)
    raw_text = json.dumps(body, ensure_ascii=False)
    raw_tokens = max(1, len(raw_text) // 3)

    tools = body.get("tools", [])
    tool_names: list[str] = []
    if isinstance(tools, list):
        for t in tools:
            if isinstance(t, dict):
                fn = t.get("function", {})
                if isinstance(fn, dict) and fn.get("name"):
                    tool_names.append(str(fn["name"]))

    diff_mode = str(getattr(delta, "diff_mode", "") or "")
    snapshot = getattr(state, "context_index_snapshot", None) if state else None
    use_incremental = (
        snapshot
        and isinstance(snapshot, dict)
        and snapshot
        and diff_mode in ("append_only", "hash_diff", "first_request")
        and delta is not None
        and getattr(delta, "added", None)
    )

    full_scan_modules: list[str] = []
    if use_incremental:
        idx = ContextIndex.from_snapshot(
            snapshot,
            req_id,
            raw_tokens=raw_tokens,
            message_count=len(messages),
            tool_count=len(tool_names),
            has_tools=bool(tool_names),
            query=query,
        )
        idx.tool_names = tool_names
        idx.tool_count = len(tool_names)
        idx.has_tools = bool(tool_names)
        for dm in delta.added:
            if 0 <= dm.index < len(messages) and isinstance(messages[dm.index], dict):
                _apply_message_to_index(idx, messages[dm.index], dm.index)
        idx.recent_files = list(dict.fromkeys(idx.recent_files))[:12]
        idx.file_refs = list(dict.fromkeys(idx.file_refs))[:20]
        idx.tool_results = idx.tool_results[-30:]
        context_mode = "incremental"
    else:
        idx = _rebuild_context_index(messages, req_id, query, raw_tokens, tool_names)
        context_mode = "rebuild"
        full_scan_modules.append("context_index")

    if state is not None:
        state.context_index_snapshot = idx.to_snapshot()
        if getattr(state, "last_ingest_metrics", None):
            state.last_ingest_metrics["context_index_mode"] = context_mode
            if full_scan_modules:
                existing = list(state.last_ingest_metrics.get("full_scan_modules") or [])
                state.last_ingest_metrics["full_scan_modules"] = list(
                    dict.fromkeys(existing + full_scan_modules)
                )

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    (INDEX_DIR / f"{req_id}.json").write_text(
        json.dumps(
            {
                "req_id": idx.req_id,
                "query": idx.query,
                "raw_tokens": idx.raw_tokens,
                "message_count": idx.message_count,
                "tool_count": idx.tool_count,
                "context_index_mode": context_mode,
                **idx.to_snapshot(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return idx


def est_tokens(text: str) -> int:
    return max(0, len(text) // 3)
