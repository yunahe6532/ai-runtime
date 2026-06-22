#!/usr/bin/env python3
"""Generate docs/PROJECT_STRUCTURE.md — source-centric logical tree (no vendor/tmp)."""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from runtime_kernel.project_index import (  # noqa: E402
    DEFAULT_EXCLUDE_DIRS,
    PathClass,
    classify_path,
    path_included_in_index,
)
from runtime_kernel.runtime_paths import repo_root  # noqa: E402

REPO = repo_root()
OUT = REPO / "docs" / "PROJECT_STRUCTURE.md"
MAX_DEPTH = 3
DETAIL_DIRS = frozenset({
    "router", "runtime_kernel", "agent_brain", "runtime_core", "docs", "scripts", "config", "tests",
})


class Node:
    __slots__ = ("dirs", "files")

    def __init__(self) -> None:
        self.dirs: dict[str, Node] = {}
        self.files: list[str] = []


def _skip_dir(name: str, rel: str) -> bool:
    if name in DEFAULT_EXCLUDE_DIRS:
        return True
    if name.startswith(".") and name not in {".github"}:
        return True
    return classify_path(rel) in (
        PathClass.VENDOR, PathClass.RUNTIME_DATA, PathClass.CACHE, PathClass.GENERATED, PathClass.GIT_METADATA,
    )


def build_logical_tree() -> tuple[Node, Counter[str]]:
    tree = Node()
    excluded: Counter[str] = Counter()
    for dirpath, dirnames, filenames in os.walk(REPO):
        rel_dir = str(Path(dirpath).relative_to(REPO))
        if rel_dir != ".":
            top = rel_dir.split("/")[0]
            pc = classify_path(rel_dir)
            if pc in (PathClass.VENDOR, PathClass.RUNTIME_DATA, PathClass.CACHE, PathClass.GENERATED):
                excluded[top] += sum(1 for _ in os.walk(dirpath) for __ in _)
                dirnames.clear()
                continue
        dirnames[:] = sorted(
            [d for d in dirnames if not _skip_dir(d, f"{rel_dir}/{d}" if rel_dir != '.' else d)],
            key=str.lower,
        )
        node = tree
        if rel_dir != ".":
            for part in rel_dir.split("/"):
                node = node.dirs.setdefault(part, Node())
        depth = len(rel_dir.split("/")) if rel_dir != "." else 0
        for fname in sorted(filenames, key=str.lower):
            rel = f"{rel_dir}/{fname}" if rel_dir != "." else fname
            if not path_included_in_index(rel):
                top = rel.split("/")[0]
                excluded[top] += 1
                continue
            if depth >= MAX_DEPTH and rel_dir.split("/")[0] not in DETAIL_DIRS:
                continue
            node.files.append(fname)
    return tree, excluded


def render(node: Node, prefix: str = "", depth: int = 0) -> list[str]:
    lines: list[str] = []
    entries: list[tuple[str, str, Node | None]] = []
    for d, child in sorted(node.dirs.items(), key=lambda x: x[0].lower()):
        entries.append(("dir", d, child))
    for f in sorted(node.files, key=str.lower):
        entries.append(("file", f, None))
    for i, (kind, name, child) in enumerate(entries):
        is_last = i == len(entries) - 1
        branch = "└── " if is_last else "├── "
        if kind == "dir":
            lines.append(f"{prefix}{branch}{name}/")
            ext = "    " if is_last else "│   "
            child_depth = depth + 1
            if child_depth < MAX_DEPTH or name in DETAIL_DIRS:
                lines.extend(render(child, prefix + ext, child_depth))
        else:
            lines.append(f"{prefix}{branch}{name}")
    return lines


def main() -> int:
    tree, excluded = build_logical_tree()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    body = ["cursor-local-llm/"]
    body.extend(render(tree))

    lines = [
        "# Project Structure",
        "",
        f"> Generated: {ts}",
        f"> Root: `{REPO}`",
        "",
        "Source-centric logical tree (vendor, tmp, cache, node_modules excluded).",
        "",
        "## Included Logical Tree",
        "",
        "```",
        *body,
        "```",
        "",
        "## Excluded From Runtime Index",
        "",
        "| path | reason | files (approx) |",
        "|------|--------|---------------:|",
    ]
    reasons = {
        "scripts": "vendor (pdf-export/node_modules)",
        ".venv-llamaindex": "venv",
        "tmp": "runtime cache",
        "ui": "node_modules + build artifacts",
        ".git": "git metadata",
    }
    for name, cnt in excluded.most_common(15):
        reason = reasons.get(name, classify_path(name).value)
        lines.append(f"| `{name}` | {reason} | {cnt:,} |")

    lines += [
        "",
        "Full inventory: `docs/reports/FILE_TREE.full.md` (optional, not indexed).",
        "",
        "*Regenerate: `python3 scripts/generate-project-structure.py`*",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Written: {OUT} ({len(body)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
