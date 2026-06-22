"""Capture incoming OpenAI-style requests for offline analysis."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger("router.capture")

CAPTURE_DIR = Path(os.getenv("CAPTURE_DIR", "/captures"))
CAPTURE_ENABLED = os.getenv("CAPTURE_REQUESTS", "0") == "1"
CAPTURE_MAX_BODY_BYTES = int(os.getenv("CAPTURE_MAX_BODY_BYTES", "50000000"))
_lock = threading.Lock()
_seq = 0


def _next_id() -> str:
    global _seq
    with _lock:
        _seq += 1
        return f"{int(time.time())}_{_seq:04d}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "image_url":
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _paragraph_hashes(text: str, min_len: int = 80) -> list[dict[str, Any]]:
    chunks = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    out: list[dict[str, Any]] = []
    for chunk in chunks:
        if len(chunk) < min_len:
            continue
        out.append({"chars": len(chunk), "hash": _sha256(chunk)[:16], "preview": chunk[:120]})
    return out


def _message_stats(msg: dict[str, Any], index: int) -> dict[str, Any]:
    role = str(msg.get("role", "unknown"))
    name = msg.get("name")
    text = _content_text(msg.get("content", ""))
    return {
        "index": index,
        "role": role,
        "name": name,
        "chars": len(text),
        "est_tokens": max(1, len(text) // 3),
        "hash": _sha256(text)[:16],
        "preview": text[:200].replace("\n", "\\n"),
        "paragraph_hashes": _paragraph_hashes(text),
        "file_refs": sorted(set(re.findall(r"(?:^|[\s`'\"])([\w./-]+\.(?:py|ts|tsx|js|jsx|md|yml|yaml|json|sh|go|rs|java|kt|cpp|h|toml))", text, re.I))),
    }


def summarize_request(path: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    msg_stats = [_message_stats(m, i) for i, m in enumerate(messages) if isinstance(m, dict)]
    total_chars = sum(m["chars"] for m in msg_stats)
    return {
        "path": path,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stream": bool(body.get("stream")),
        "model": body.get("model"),
        "message_count": len(msg_stats),
        "total_chars": total_chars,
        "est_tokens": max(1, total_chars // 3),
        "roles": {role: sum(1 for m in msg_stats if m["role"] == role) for role in sorted({m["role"] for m in msg_stats})},
        "messages": msg_stats,
        "header_keys": sorted(headers.keys()),
        "tools_present": "tools" in body,
        "tool_count": len(body.get("tools", [])) if isinstance(body.get("tools"), list) else 0,
    }


def maybe_capture(path: str, body_bytes: bytes, body_json: dict[str, Any] | None, headers: dict[str, str]) -> str | None:
    if not CAPTURE_ENABLED:
        return None
    if not path.startswith("/v1/chat/completions"):
        return None
    if not body_json:
        return None
    if len(body_bytes) > CAPTURE_MAX_BODY_BYTES:
        LOG.warning("capture skipped: body too large (%d bytes)", len(body_bytes))
        return None

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    capture_id = _next_id()
    raw_path = CAPTURE_DIR / f"{capture_id}.request.json"
    summary_path = CAPTURE_DIR / f"{capture_id}.summary.json"

    payload = {
        "id": capture_id,
        "path": path,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "headers": {k: v for k, v in headers.items()},
        "body": body_json,
    }
    summary = summarize_request(path, body_json, headers)
    summary["id"] = capture_id

    with _lock:
        raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    LOG.info(
        "captured request %s msgs=%d chars=%d est_tokens=%d tools=%s",
        capture_id,
        summary["message_count"],
        summary["total_chars"],
        summary["est_tokens"],
        summary["tool_count"],
    )
    return capture_id
