#!/usr/bin/env python3
"""Generate docs/reports/FILE_TREE.full.md — full project file inventory (not indexed)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "reports" / "FILE_TREE.full.md"


class Node:
    __slots__ = ("dirs", "files")

    def __init__(self) -> None:
        self.dirs: dict[str, Node] = {}
        self.files: list[str] = []


def build_tree(root: Path) -> tuple[Node, int]:
    tree = Node()
    file_count = 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        node = tree
        if str(rel_dir) != ".":
            for part in rel_dir.parts:
                node = node.dirs.setdefault(part, Node())

        dirnames.sort(key=str.lower)
        filenames.sort(key=str.lower)

        for d in dirnames:
            node.dirs.setdefault(d, Node())
        for f in filenames:
            node.files.append(f)
            file_count += 1

    return tree, file_count


def count_dirs(node: Node) -> int:
    return len(node.dirs) + sum(count_dirs(child) for child in node.dirs.values())


def depth1_stats(root: Path) -> dict[str, int]:
    stats: dict[str, int] = {}
    for entry in root.iterdir():
        if entry.is_dir():
            stats[entry.name] = sum(1 for _, _, files in os.walk(entry) for _ in files)
        else:
            stats.setdefault("(root)", 0)
            stats["(root)"] += 1
    return stats


def render(node: Node, prefix: str = "") -> list[str]:
    lines: list[str] = []
    entries: list[tuple[str, str, Node | None]] = []
    for dname, child in sorted(node.dirs.items(), key=lambda x: x[0].lower()):
        entries.append(("dir", dname, child))
    for fname in sorted(node.files, key=str.lower):
        entries.append(("file", fname, None))

    for i, (kind, name, child) in enumerate(entries):
        is_last = i == len(entries) - 1
        branch = "└── " if is_last else "├── "
        if kind == "dir":
            lines.append(f"{prefix}{branch}{name}/")
            extension = "    " if is_last else "│   "
            lines.extend(render(child, prefix + extension))
        else:
            lines.append(f"{prefix}{branch}{name}")
    return lines


def main() -> None:
    tree, file_count = build_tree(ROOT)
    dir_count = count_dirs(tree)
    stats = depth1_stats(ROOT)

    header = f"""# cursor-local-llm 파일 전수조사

> 생성일: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}  
> 루트: `{ROOT}`  
> 디렉터리: **{dir_count:,}**개 · 파일: **{file_count:,}**개 · 총 항목: **{dir_count + file_count:,}**개

## 디렉터리별 파일 수 (1depth)

| 디렉터리 | 파일 수 |
|----------|--------:|
"""
    for name, cnt in sorted(stats.items(), key=lambda x: (-x[1], x[0].lower())):
        header += f"| `{name}` | {cnt:,} |\n"

    body = ["cursor-local-llm/"]
    body.extend(render(tree))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n## 전체 파일 트리\n\n```\n")
        f.write("\n".join(body))
        f.write("\n```\n\n---\n\n")
        f.write("*재생성: `python3 scripts/generate-file-tree.py` → `docs/reports/FILE_TREE.full.md`*\n")

    print(f"Written: {OUT}")
    print(f"Lines: {len(body)}")
    print(f"Size: {OUT.stat().st_size / 1024 / 1024:.2f} MB")


if __name__ == "__main__":
    main()
