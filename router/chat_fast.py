"""Simple Q&A fast path: strip agent context and forward plain chat."""

from __future__ import annotations

import copy
import os
import re
from typing import Any

from capture import _content_text

SIMPLE_QA_MAX_CHARS = int(os.getenv("SIMPLE_QA_MAX_CHARS", "500"))
SIMPLE_QA_MAX_TOKENS = int(os.getenv("SIMPLE_QA_MAX_TOKENS", "256"))

SYSTEM_CHAT = (
    "Answer directly and briefly in plain text. "
    "Do not use tools, shell commands, files, logs, or prior project context. "
    "For counting or simple reasoning, compute directly. "
    "Do not continue previous tasks unless the user explicitly asks."
)

HARD_TASK_KEYWORDS = [
    "수정",
    "구현",
    "파일",
    "코드",
    "에러",
    "에러 로그",
    "docker log",
    "docker logs",
    "bash",
    "grep",
    "repo",
    "커밋",
    "디버깅",
    "실행",
    "패치",
    "fix",
    "implement",
    "file",
    "code",
    "error",
    "log",
    "debug",
    "router",
    "compose",
    "benchmark",
    "캡처",
    "capture",
    "optimizer",
    "lite pack",
    "cloudflare",
    "스크립트",
    "script",
    "서버",
    "벤치",
    "벤치마킹",
    "분석",
    "확인하고",
    "확인해",
    "짜서",
    "작성해",
    "검색",
    "측정",
]

_SKIP_MARKERS = (
    "<attached_files>",
    "<image_files>",
    "data:image",
    "<agent_transcripts>",
    "<open_and_recently_viewed_files>",
)


def _extract_user_query(text: str) -> str:
    m = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.S)
    if m:
        return m.group(1).strip()
    return text.strip()


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            query_parts: list[str] = []
            fallback_parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "image_url":
                    return ""
                if block.get("type") != "text":
                    continue
                raw = str(block.get("text", "")).strip()
                if not raw:
                    continue
                if "<user_query>" in raw:
                    query_parts.append(_extract_user_query(raw))
                    continue
                lower = raw.lower()
                if any(marker.lower() in lower for marker in _SKIP_MARKERS):
                    continue
                fallback_parts.append(_extract_user_query(raw))
            if query_parts:
                return query_parts[-1].strip()
            if fallback_parts:
                return fallback_parts[-1].strip()
            return ""
        if isinstance(content, str):
            return _extract_user_query(content)
    return ""


def is_simple_qa(messages: list[dict[str, Any]]) -> bool:
    if not messages:
        return False

    text = _last_user_text(messages)
    if not text:
        return False

    if len(text) > SIMPLE_QA_MAX_CHARS:
        return False

    lower = text.lower()
    if any(marker.lower() in lower for marker in _SKIP_MARKERS):
        return False

    if any(k in lower for k in HARD_TASK_KEYWORDS):
        return False

    return True


READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset({"Read", "Grep", "Glob", "ReadLints", "WebSearch", "WebFetch", "FetchMcpResource"})


def strip_agent_fields(body: dict[str, Any], keep_readonly: bool = False) -> None:
    """Remove agentic fields. If keep_readonly=True, preserve Read/Grep tools instead of stripping all."""
    if keep_readonly:
        tools = body.get("tools")
        if isinstance(tools, list):
            readonly_tools = [t for t in tools if isinstance(t, dict) and
                              (t.get("function") or {}).get("name") in READ_ONLY_TOOL_NAMES]
            if readonly_tools:
                body["tools"] = readonly_tools
                body.pop("tool_choice", None)
                body.pop("parallel_tool_calls", None)
                body.pop("functions", None)
                body.pop("function_call", None)
                return
    for key in ("tools", "tool_choice", "parallel_tool_calls", "functions", "function_call"):
        body.pop(key, None)


def build_simple_chat_body(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages", [])
    user_text = _last_user_text(messages if isinstance(messages, list) else [])
    if not user_text:
        out = copy.deepcopy(body)
        strip_agent_fields(out)
        return out

    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        max_tokens = SIMPLE_QA_MAX_TOKENS
    else:
        max_tokens = min(max_tokens, SIMPLE_QA_MAX_TOKENS)

    out: dict[str, Any] = {
        "model": body.get("model", "model.gguf"),
        "stream": body.get("stream", True),
        "temperature": body.get("temperature", 0.2),
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": SYSTEM_CHAT},
            {"role": "user", "content": user_text},
        ],
    }
    return out


def est_message_chars(body: dict[str, Any]) -> int:
    msgs = body.get("messages", [])
    if not isinstance(msgs, list):
        return 0
    return sum(len(_content_text(m.get("content", ""))) for m in msgs if isinstance(m, dict))
