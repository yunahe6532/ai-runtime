#!/usr/bin/env python3
"""Tests for rule-based artifact excerpts (chunk + merge, no preview-as-summary)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from artifact_excerpt import build_grep_excerpt, build_prompt_excerpt  # noqa: E402


def _fake_grep(n_files: int = 80) -> str:
    lines = ['<workspace_result workspace_path="/home/yunahe">']
    for i in range(n_files):
        path = f"router/adapters/module_{i}.py"
        lines.append(path)
        lines.append(f'  1:"""Module {i} — adapter glue for backend {i}."""')
        lines.append(f"  2:from __future__ import annotations")
    lines.append("</workspace_result>")
    return "\n".join(lines)


def test_grep_excerpt_lists_all_files_in_chunks() -> None:
    text = _fake_grep(80)
    merged, chunks = build_grep_excerpt(text, path="/app/adapters", max_chars=20000)
    assert len(chunks) >= 2, len(chunks)
    assert "module_0.py" in merged
    assert "module_79.py" in merged
    assert "chunk=1/" in merged
    assert "adapter glue" in merged
    assert "<workspace_result" not in merged
    print("test_grep_excerpt_lists_all_files_in_chunks: OK")


def test_prompt_excerpt_not_head_preview() -> None:
    text = _fake_grep(50)
    merged, _ = build_prompt_excerpt(text, path="/app/adapters", tool_name="Grep", art_type="file_read")
    assert "module_40.py" in merged or "module_49.py" in merged
    assert len(merged) > 1500
    print("test_prompt_excerpt_not_head_preview: OK")


def main() -> int:
    test_grep_excerpt_lists_all_files_in_chunks()
    test_prompt_excerpt_not_head_preview()
    print("ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
