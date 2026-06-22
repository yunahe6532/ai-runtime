"""VL pre-pass: image → text evidence → Coder handoff."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from typing import Any, Callable

LOG = logging.getLogger("router.vl_pass")

VL_PASS_ENABLED = os.getenv("VL_PASS_ENABLED", "1") == "1"
VL_PASS_MAX_TOKENS = int(os.getenv("VL_PASS_MAX_TOKENS", "1024"))
VL_PASS_MAX_IMAGES = int(os.getenv("VL_PASS_MAX_IMAGES", "4"))

IMAGE_PART_TYPES = frozenset({"image_url", "input_image", "image"})
MIN_IMAGE_URL_LEN = int(os.getenv("MIN_IMAGE_URL_LEN", "50"))


def _image_url_from_part(part: dict[str, Any]) -> str:
    t = part.get("type")
    if t == "image_url":
        iu = part.get("image_url")
        if isinstance(iu, dict):
            return str(iu.get("url") or "").strip()
        return str(iu or "").strip()
    if t == "input_image":
        img = part.get("input_image")
        if isinstance(img, dict):
            return str(img.get("data") or img.get("url") or "").strip()
        return str(img or "").strip()
    if t == "image":
        for key in ("url", "data", "image"):
            val = part.get(key)
            if val:
                return str(val).strip()
    return ""


def is_valid_image_part(part: dict[str, Any]) -> bool:
    """True only when an image part carries non-empty payload."""
    if not isinstance(part, dict) or part.get("type") not in IMAGE_PART_TYPES:
        return False
    url = _image_url_from_part(part)
    if len(url) < MIN_IMAGE_URL_LEN:
        return False
    lower = url.lower()
    if lower in ("", "null", "none"):
        return False
    return lower.startswith("data:image/") or lower.startswith("http://") or lower.startswith("https://")


def has_image_content(messages: list[dict[str, Any]]) -> bool:
    """Backward-compatible alias: only real image payloads count."""
    return has_real_image_content(messages)


def has_real_image_content(messages: list[dict[str, Any]]) -> bool:
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if is_valid_image_part(part):
                    return True
    return False


def last_user_message_has_image(messages: list[dict[str, Any]]) -> bool:
    """True only if the MOST RECENT user message contains a real image.

    Images in earlier turns are already processed (or irrelevant) — triggering
    another VL pass for them on every follow-up text message is wasteful.
    """
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                return any(is_valid_image_part(part) for part in content)
            return False  # last user message is plain text — no image
    return False


def count_images(messages: list[dict[str, Any]]) -> int:
    n = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if is_valid_image_part(part):
                    n += 1
    return n


VL_SYSTEM_PROMPT = (
    "You are an image evidence extractor for a coding agent.\n"
    "Analyze the image(s) and output ONLY a valid JSON object with these keys:\n"
    "  image_summary: string — what the image shows\n"
    "  visible_error_text: string[] — any error messages visible\n"
    "  visible_files_or_paths: string[] — file paths or names visible\n"
    "  ui_state: string — UI state description\n"
    "  likely_task: string — what the user probably wants done\n"
    "  confidence: high|medium|low\n"
    "  coder_instructions: string[] — what the coder should do next\n"
    "Output ONLY the JSON object. No markdown fences. No prose."
)

CODER_EVIDENCE_FOOTER = (
    "\n\n[Important]\n"
    "The coder model cannot see the image. Treat the evidence above as hints only. "
    "Verify with Read, Grep, or Shell before making changes."
)


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return ""


def build_vl_pass_body(messages: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """Build a VL-only request keeping image parts from recent user messages."""
    vl_messages: list[dict[str, Any]] = [
        {"role": "system", "content": VL_SYSTEM_PROMPT},
    ]

    image_parts: list[dict[str, Any]] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if is_valid_image_part(part):
                image_parts.insert(0, part)
        if image_parts:
            break

    image_parts = image_parts[:VL_PASS_MAX_IMAGES]
    user_content: list[dict[str, Any]] = [{"type": "text", "text": query or "Analyze the image(s)."}]
    user_content.extend(image_parts)

    vl_messages.append({"role": "user", "content": user_content})
    return {
        "model": "model.gguf",
        "stream": False,
        "max_tokens": VL_PASS_MAX_TOKENS,
        "messages": vl_messages,
    }


def parse_vl_evidence(raw: str) -> dict[str, Any]:
    """Parse VL JSON output; fall back to plain text wrapper."""
    text = raw.strip()
    if not text:
        return {"image_summary": "(empty VL response)", "confidence": "low", "coder_instructions": []}

    # Strip markdown fences if model disobeyed
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)

    # Find first JSON object
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    return {
        "image_summary": text[:2000],
        "visible_error_text": [],
        "visible_files_or_paths": [],
        "ui_state": "",
        "likely_task": "",
        "confidence": "low",
        "coder_instructions": ["Verify with files and tools; VL output was not valid JSON."],
        "_raw": text[:1000],
    }


def format_evidence_block(evidence: dict[str, Any]) -> str:
    lines = ["[Image Evidence from VL]"]
    if evidence.get("image_summary"):
        lines.append(f"Summary: {evidence['image_summary']}")
    errors = evidence.get("visible_error_text") or []
    if errors:
        lines.append("Visible errors: " + "; ".join(str(e) for e in errors[:10]))
    paths = evidence.get("visible_files_or_paths") or []
    if paths:
        lines.append("Visible paths: " + ", ".join(str(p) for p in paths[:10]))
    if evidence.get("ui_state"):
        lines.append(f"UI state: {evidence['ui_state']}")
    if evidence.get("likely_task"):
        lines.append(f"Likely task: {evidence['likely_task']}")
    lines.append(f"Confidence: {evidence.get('confidence', 'unknown')}")
    instructions = evidence.get("coder_instructions") or []
    if instructions:
        lines.append("Suggested next steps:")
        for inst in instructions[:8]:
            lines.append(f"  - {inst}")
    return "\n".join(lines) + CODER_EVIDENCE_FOOTER


def strip_images_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove image parts; keep text-only content for coder model."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        m = copy.deepcopy(msg)
        content = m.get("content")
        if isinstance(content, list):
            text_parts = [
                str(p.get("text", "")).strip()
                for p in content
                if isinstance(p, dict) and p.get("type") == "text" and str(p.get("text", "")).strip()
            ]
            m["content"] = "\n".join(text_parts) if text_parts else ""
        out.append(m)
    return out


def normalize_messages_for_coder(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """
    Flatten Cursor multimodal message format for text-only Coder backends.

    Cursor often sends content as:
      [{"type":"text","text":"..."}]   # even with no image
    llama-server text models expect string content, not arrays.
    """
    out = copy.deepcopy(body)
    messages = out.get("messages", [])
    if not isinstance(messages, list):
        return out, {"flattened": 0, "stripped_image_parts": 0}

    flattened = 0
    stripped_images = 0
    normalized: list[dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        m = copy.deepcopy(msg)
        content = m.get("content")
        if isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if is_valid_image_part(part):
                    stripped_images += 1
                    continue
                if part.get("type") == "text":
                    t = str(part.get("text", "")).strip()
                    if t:
                        text_parts.append(t)
            m["content"] = "\n".join(text_parts) if text_parts else ""
            flattened += 1
        normalized.append(m)

    out["messages"] = normalized
    return out, {"flattened": flattened, "stripped_image_parts": stripped_images}


def message_has_image(msg: dict[str, Any]) -> bool:
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(is_valid_image_part(p) for p in content if isinstance(p, dict))


def normalize_messages_for_multimodal(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """
    Prepare Cursor messages for llama-server mmproj backends.

    - Text-only content arrays → string (OpenAI compat)
    - Messages with images → keep image_url parts + text parts as array
    """
    out = copy.deepcopy(body)
    messages = out.get("messages", [])
    if not isinstance(messages, list):
        return out, {"flattened": 0, "kept_image_parts": 0}

    flattened = 0
    kept_images = 0
    normalized: list[dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        m = copy.deepcopy(msg)
        content = m.get("content")
        if isinstance(content, list):
            has_image = any(is_valid_image_part(p) for p in content if isinstance(p, dict))
            if has_image:
                parts: list[dict[str, Any]] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if is_valid_image_part(part):
                        parts.append(copy.deepcopy(part))
                        kept_images += 1
                    elif part.get("type") == "text":
                        t = str(part.get("text", "")).strip()
                        if t:
                            parts.append({"type": "text", "text": t})
                m["content"] = parts
            else:
                text_parts: list[str] = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        t = str(part.get("text", "")).strip()
                        if t:
                            text_parts.append(t)
                m["content"] = "\n".join(text_parts) if text_parts else ""
            flattened += 1
        normalized.append(m)

    out["messages"] = normalized
    return out, {"flattened": flattened, "kept_image_parts": kept_images}


def inject_evidence_into_body(body: dict[str, Any], evidence_text: str) -> dict[str, Any]:
    """Strip images and append VL evidence to the last user message."""
    out = copy.deepcopy(body)
    messages = out.get("messages", [])
    if not isinstance(messages, list):
        return out

    messages = strip_images_from_messages(messages)

    # Append evidence to last user message
    injected = False
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, dict) and msg.get("role") == "user":
            existing = _text_from_content(msg.get("content", ""))
            msg["content"] = existing + "\n\n" + evidence_text if existing else evidence_text
            injected = True
            break

    if not injected:
        messages.append({"role": "user", "content": evidence_text})

    out["messages"] = messages
    return out


def run_vl_evidence_pass(
    body: dict[str, Any],
    vl_url: str,
    query: str,
    call_fn: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Run VL inference and return (formatted_evidence_text, parsed_evidence_dict).
    call_fn(url, vl_body) -> response_json; defaults to httpx POST.
    """
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        raise ValueError("body has no messages list")

    vl_body = build_vl_pass_body(messages, query)
    img_count = count_images(messages)

    if call_fn is None:
        import httpx
        def _default_call(url: str, payload: dict[str, Any]) -> dict[str, Any]:
            r = httpx.post(f"{url}/v1/chat/completions", json=payload, timeout=120.0)
            r.raise_for_status()
            return r.json()
        call_fn = _default_call

    LOG.info("vl_pass starting images=%d query_chars=%d", img_count, len(query))
    t0_import = __import__("time").perf_counter()
    resp = call_fn(vl_url, vl_body)
    elapsed = __import__("time").perf_counter() - t0_import

    raw_content = ""
    try:
        raw_content = str(resp["choices"][0]["message"].get("content") or "")
    except (KeyError, IndexError, TypeError):
        raw_content = json.dumps(resp)[:2000]

    evidence = parse_vl_evidence(raw_content)
    block = format_evidence_block(evidence)
    LOG.info(
        "vl_pass done in %.2fs images=%d evidence_chars=%d confidence=%s",
        elapsed,
        img_count,
        len(block),
        evidence.get("confidence", "?"),
    )
    return block, evidence


def maybe_vl_preprocess(
    body: dict[str, Any],
    query: str,
    vl_url: str,
    switch_to_vl: Callable[[], None],
    call_fn: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], bool]:
    """
    If images present: switch to VL, extract evidence, return coder-ready body.
    Returns (body, vl_pass_ran).
    """
    if not VL_PASS_ENABLED:
        return body, False

    messages = body.get("messages", [])
    if not isinstance(messages, list) or not last_user_message_has_image(messages):
        return body, False

    switch_to_vl()
    evidence_text, _ = run_vl_evidence_pass(body, vl_url, query, call_fn=call_fn)
    return inject_evidence_into_body(body, evidence_text), True
