"""Incremental message indexing — stable keys, append-only diff, kind classification."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from capture import _content_text, _sha256

LOG = logging.getLogger("router.message_index")

MessageKind = Literal[
    "user_task",
    "user_meta",
    "assistant_tool_call",
    "tool_result",
    "assistant_final",
    "system_reminder",
    "continuation_notice",
    "noise",
    "unknown",
]

DiffMode = Literal["append_only", "hash_diff", "rebuild", "count_shrink", "first_request"]

CONTINUATION_PATTERNS = (
    "your response was cut off",
    "exceeded the output token limit",
    "continue from where you left off",
    "output token limit",
)
NOISE_PATTERNS = (
    "system_reminder",
    "you are now in agent mode",
    "you are now in ask mode",
)


def normalize_content(content: Any) -> str:
    return _content_text(content).strip()


def stable_message_key(msg: dict[str, Any]) -> str:
    """Single source of truth for message identity across router modules."""
    role = str(msg.get("role", ""))

    if role == "tool":
        tcid = str(msg.get("tool_call_id") or "").strip()
        if tcid:
            return f"tool:{tcid}"
        name = str(msg.get("name") or "tool")
        blob = f"{name}|{normalize_content(msg.get('content', ''))}"
        return f"tool:{_sha256(blob)[:16]}"

    if role == "assistant" and msg.get("tool_calls"):
        tc_json = json.dumps(msg.get("tool_calls"), sort_keys=True, ensure_ascii=False)
        return f"assistant_tool_calls:{_sha256(tc_json)[:16]}"

    if role in ("user", "system"):
        return f"{role}:{_sha256(normalize_content(msg.get('content', '')))[:16]}"

    payload = json.dumps(
        {
            "role": role,
            "name": msg.get("name"),
            "content": normalize_content(msg.get("content", "")),
            "tool_call_id": msg.get("tool_call_id"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return f"{role}:{_sha256(payload)[:16]}"


def stable_message_key_short(msg: dict[str, Any]) -> str:
    """16-char suffix for backward-compatible fingerprint lists."""
    return stable_message_key(msg).rsplit(":", 1)[-1][:16]


def classify_message_kind(msg: dict[str, Any]) -> MessageKind:
    role = str(msg.get("role", ""))
    text = normalize_content(msg.get("content", ""))
    lower = text.lower()

    if role == "tool":
        return "tool_result"

    if role == "assistant":
        if msg.get("tool_calls"):
            return "assistant_tool_call"
        if text:
            return "assistant_final"
        return "noise"

    if role == "user":
        if any(p in lower for p in CONTINUATION_PATTERNS):
            return "continuation_notice"
        if "<user_query>" in text:
            return "user_task"
        if any(p in lower for p in NOISE_PATTERNS):
            return "system_reminder"
        if "<user_info>" in text or "<user_rules>" in text or "<rules>" in text:
            return "user_meta"
        if text.strip():
            return "user_task"
        return "noise"

    if role == "system":
        if any(p in lower for p in CONTINUATION_PATTERNS):
            return "continuation_notice"
        if "reminder" in lower or "system_reminder" in lower:
            return "system_reminder"
        return "user_meta"

    return "unknown"


def is_noise_kind(kind: MessageKind) -> bool:
    return kind in ("system_reminder", "continuation_notice", "noise")


@dataclass
class IndexedMessage:
    key: str
    role: str
    kind: MessageKind
    index: int
    tool_call_id: str = ""
    token_est: int = 0
    cold: bool = False
    preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MessageDiff:
    new_messages: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    new_indexed: list[IndexedMessage] = field(default_factory=list)
    all_keys: list[str] = field(default_factory=list)
    mode: DiffMode = "first_request"
    messages_total: int = 0
    messages_new: int = 0
    prefix_len: int = 0

    def to_metrics(self) -> dict[str, Any]:
        return {
            "messages_total": self.messages_total,
            "messages_new": self.messages_new,
            "diff_mode": self.mode,
            "prefix_len": self.prefix_len,
        }


def index_message(msg: dict[str, Any], index: int, *, cold: bool = False) -> IndexedMessage:
    text = normalize_content(msg.get("content", ""))
    kind = classify_message_kind(msg)
    return IndexedMessage(
        key=stable_message_key(msg),
        role=str(msg.get("role", "")),
        kind=kind,
        index=index,
        tool_call_id=str(msg.get("tool_call_id") or ""),
        token_est=max(1, len(text) // 3),
        cold=cold,
        preview=text[:120].replace("\n", " "),
    )


def diff_messages(
    messages: list[Any],
    prev_keys: list[str],
    *,
    count_shrink: bool = False,
    shrink_start: int = 0,
) -> MessageDiff:
    """Return only new messages since prev_keys. Supports append-only fast path."""
    dict_msgs = [m for m in messages if isinstance(m, dict)]
    all_keys = [stable_message_key(m) for m in dict_msgs]
    total = len(dict_msgs)

    if not prev_keys:
        indexed = [index_message(m, i) for i, m in enumerate(dict_msgs)]
        return MessageDiff(
            new_messages=[(i, m) for i, m in enumerate(dict_msgs)],
            new_indexed=indexed,
            all_keys=all_keys,
            mode="first_request",
            messages_total=total,
            messages_new=total,
            prefix_len=0,
        )

    if count_shrink:
        start = max(0, shrink_start)
        new_pairs = [(start + i, m) for i, m in enumerate(dict_msgs[start:])]
        indexed = [index_message(m, idx) for idx, m in new_pairs]
        return MessageDiff(
            new_messages=new_pairs,
            new_indexed=indexed,
            all_keys=all_keys,
            mode="count_shrink",
            messages_total=total,
            messages_new=len(new_pairs),
            prefix_len=start,
        )

    prefix_len = len(prev_keys)
    if total >= prefix_len and all_keys[:prefix_len] == prev_keys:
        new_pairs = [(prefix_len + i, m) for i, m in enumerate(dict_msgs[prefix_len:])]
        indexed = [index_message(m, idx) for idx, m in new_pairs]
        return MessageDiff(
            new_messages=new_pairs,
            new_indexed=indexed,
            all_keys=all_keys,
            mode="append_only",
            messages_total=total,
            messages_new=len(new_pairs),
            prefix_len=prefix_len,
        )

    prev_set = set(prev_keys)
    new_pairs: list[tuple[int, dict[str, Any]]] = []
    indexed: list[IndexedMessage] = []
    for i, m in enumerate(dict_msgs):
        key = all_keys[i]
        if key not in prev_set:
            new_pairs.append((i, m))
            indexed.append(index_message(m, i))

    mode: DiffMode = "hash_diff" if new_pairs else "rebuild"
    if not new_pairs and all_keys != prev_keys:
        mode = "rebuild"
        new_pairs = [(i, m) for i, m in enumerate(dict_msgs)]
        indexed = [index_message(m, i) for i, m in new_pairs]

    return MessageDiff(
        new_messages=new_pairs,
        new_indexed=indexed,
        all_keys=all_keys,
        mode=mode,
        messages_total=total,
        messages_new=len(new_pairs),
        prefix_len=0,
    )


def log_ingest_metrics(
    req_id: str,
    diff: MessageDiff,
    *,
    context_index_mode: str = "",
    plan_input_mode: str = "",
    phase_update_mode: str = "",
    full_scan_modules: list[str] | None = None,
) -> dict[str, Any]:
    metrics = {
        **diff.to_metrics(),
        "context_index_mode": context_index_mode,
        "plan_input_mode": plan_input_mode,
        "phase_update_mode": phase_update_mode,
        "full_scan_modules": full_scan_modules or [],
    }
    LOG.info(
        "ingest_metrics req=%s total=%d new=%d mode=%s ctx=%s plan=%s phase=%s full_scan=%s",
        req_id,
        metrics["messages_total"],
        metrics["messages_new"],
        metrics["diff_mode"],
        context_index_mode,
        plan_input_mode,
        phase_update_mode,
        ",".join(metrics["full_scan_modules"]) or "-",
    )
    return metrics
