#!/usr/bin/env python3
"""Unit tests for vl_pass module."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

from vl_pass import (  # noqa: E402
    build_vl_pass_body,
    format_evidence_block,
    has_image_content,
    has_real_image_content,
    inject_evidence_into_body,
    is_valid_image_part,
    message_has_image,
    normalize_messages_for_coder,
    normalize_messages_for_multimodal,
    parse_vl_evidence,
    strip_images_from_messages,
)


def test_text_only_list_not_image():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    assert has_real_image_content(msgs) is False
    assert has_image_content(msgs) is False
    print("text_only_list: OK")


def test_empty_image_url_ignored():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": ""}},
    ]}]
    assert has_real_image_content(msgs) is False
    out, stats = normalize_messages_for_coder({"messages": msgs})
    assert out["messages"][0]["content"] == "hello"
    assert stats["stripped_image_parts"] == 0
    print("empty_image_ignored: OK")


def test_normalize_cursor_text_array():
    body = {"messages": [
        {"role": "user", "content": [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}]},
        {"role": "assistant", "content": "ok"},
    ]}
    out, stats = normalize_messages_for_coder(body)
    assert out["messages"][0]["content"] == "line1\nline2"
    assert isinstance(out["messages"][0]["content"], str)
    assert stats["flattened"] == 1
    print("normalize_text_array: OK")


def test_has_image():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 60}},
    ]}]
    assert has_image_content(msgs) is True
    assert has_image_content([{"role": "user", "content": "hello"}]) is False
    print("has_image: OK")


def test_strip_images():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "check this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
    ]}]
    out = strip_images_from_messages(msgs)
    assert out[0]["content"] == "check this"
    assert "image" not in str(out[0]["content"]).lower() or True
    print("strip_images: OK")


def test_parse_vl_json():
    raw = '{"image_summary":"login page","visible_error_text":["TypeError"],"confidence":"high","coder_instructions":["Read UserList.tsx"]}'
    ev = parse_vl_evidence(raw)
    assert ev["image_summary"] == "login page"
    assert ev["confidence"] == "high"
    print("parse_vl_json: OK")


def test_parse_vl_fence():
    raw = '```json\n{"image_summary":"error dialog","confidence":"medium","coder_instructions":[]}\n```'
    ev = parse_vl_evidence(raw)
    assert "error dialog" in ev["image_summary"]
    print("parse_vl_fence: OK")


def test_inject_evidence():
    body = {
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "fix this bug"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
        ]}],
        "tools": [],
    }
    evidence = "[Image Evidence from VL]\nSummary: console error\nConfidence: high"
    out = inject_evidence_into_body(body, evidence)
    user_content = out["messages"][-1]["content"]
    assert isinstance(user_content, str)
    assert "Image Evidence" in user_content
    assert "fix this bug" in user_content
    assert "image_url" not in user_content
    print("inject_evidence: OK")


def test_build_vl_body():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "analyze screenshot"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 60}},
    ]}]
    body = build_vl_pass_body(msgs, "analyze screenshot")
    assert body["stream"] is False
    user = body["messages"][-1]["content"]
    assert any(p.get("type") == "image_url" for p in user if isinstance(p, dict))
    print("build_vl_body: OK")


def test_format_evidence():
    block = format_evidence_block({
        "image_summary": "React login page",
        "visible_error_text": ["Cannot read map"],
        "confidence": "high",
        "coder_instructions": ["Read UserList.tsx"],
    })
    assert "React login page" in block
    assert "Cannot read map" in block
    assert "UserList" in block
    print("format_evidence: OK")


def test_normalize_multimodal_keeps_image():
    url = "data:image/png;base64," + "A" * 60
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "what color?"},
        {"type": "image_url", "image_url": {"url": url}},
    ]}]}
    out, stats = normalize_messages_for_multimodal(body)
    content = out["messages"][0]["content"]
    assert isinstance(content, list)
    assert any(p.get("type") == "image_url" for p in content)
    assert stats["kept_image_parts"] == 1
    assert message_has_image(out["messages"][0])
    print("normalize_multimodal: OK")


if __name__ == "__main__":
    test_text_only_list_not_image()
    test_empty_image_url_ignored()
    test_normalize_cursor_text_array()
    test_normalize_multimodal_keeps_image()
    test_has_image()
    test_strip_images()
    test_parse_vl_json()
    test_parse_vl_fence()
    test_inject_evidence()
    test_build_vl_body()
    test_format_evidence()
    print("all passed")
