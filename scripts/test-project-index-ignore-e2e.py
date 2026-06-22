#!/usr/bin/env python3
"""E2E tests for Project Index ignore policy."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from runtime_kernel.project_index import (  # noqa: E402
    PathClass,
    ProjectIndexConfig,
    bootstrap_project_index,
    classify_path,
    path_included_in_index,
)


def test_classify_paths() -> None:
    assert classify_path("router/main.py") == PathClass.SOURCE
    assert classify_path("docs/VISION.md") == PathClass.DOC
    assert classify_path("scripts/foo.py") == PathClass.SCRIPT
    assert classify_path("tmp/foo.json") == PathClass.RUNTIME_DATA
    assert classify_path("ui/node_modules/x.js") == PathClass.VENDOR
    assert classify_path("docs/FILE_TREE.md") == PathClass.GENERATED
    assert classify_path(".git/config") == PathClass.GIT_METADATA
    print("PASS test_classify_paths")


def test_index_excludes_vendor_and_tmp() -> None:
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "router").mkdir()
        (ws / "router" / "main.py").write_text("print('ok')\n", encoding="utf-8")
        (ws / "tmp").mkdir()
        (ws / "tmp" / "cache.json").write_text("{}", encoding="utf-8")
        (ws / "node_modules").mkdir()
        (ws / "node_modules" / "pkg.js").write_text("//x\n", encoding="utf-8")
        (ws / "docs").mkdir()
        (ws / "docs" / "FILE_TREE.md").write_text("# big\n", encoding="utf-8")
        (ws / "docs" / "README.md").write_text("# doc\n", encoding="utf-8")

        idx = bootstrap_project_index(str(ws), "testpk", cfg=ProjectIndexConfig(max_files=100))
        rels = {f["relpath"] for f in idx.files}
        assert "router/main.py" in rels
        assert "docs/README.md" in rels
        assert "tmp/cache.json" not in rels
        assert not any("node_modules" in r for r in rels)
        assert "docs/FILE_TREE.md" not in rels
        assert idx.excluded_summary
        print("PASS test_index_excludes_vendor_and_tmp")


def test_path_included_policy() -> None:
    assert path_included_in_index("router/foo.py") is True
    assert path_included_in_index("tmp/x.py") is False
    assert path_included_in_index("docs/FILE_TREE.md") is False
    print("PASS test_path_included_policy")


def main() -> int:
    test_classify_paths()
    test_index_excludes_vendor_and_tmp()
    test_path_included_policy()
    print("\nAll project index ignore E2E tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
