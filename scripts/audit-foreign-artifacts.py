#!/usr/bin/env python3
"""Audit foreign projects, vendor deps, and non-source artifacts in repo."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from runtime_kernel.runtime_paths import audit_json_dir, repo_root, reports_dir  # noqa: E402

REPO = repo_root()

CLASSIFICATIONS = (
    "project_source",
    "project_script",
    "project_test",
    "project_doc",
    "runtime_artifact",
    "generated_artifact",
    "vendor_dependency",
    "foreign_project",
    "unknown_review",
)

KEEP_PREFIXES = (
    "router/",
    "runtime_kernel/",
    "agent_brain/",
    "observability/",
    "config/",
    "configs/",
    "tests/",
    "ui/src/",
    "ui/package.json",
    "ui/package-lock.json",
    "ui/pnpm-lock.yaml",
    "docker-compose",
    "Dockerfile",
    ".env.example",
    "README.md",
    "handoff.md",
)

PRIORITY_PATHS = [
    "scripts/pdf-export",
    "scripts/pdf-export/node_modules",
    "ui/node_modules",
    ".venv-llamaindex",
    "tmp",
    "docs/reports/FILE_TREE.full.md",
]

VENDOR_MARKERS = frozenset({
    "node_modules", ".venv", ".venv-llamaindex", "venv", ".tox",
    "__pycache__", ".pytest_cache", "dist", "build", "coverage", ".next", ".vite",
})

LOCK_FILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")


@dataclass
class ArtifactEntry:
    path: str
    classification: str
    file_count: int = 0
    bytes: int = 0
    reason: str = ""
    linked_to_project: bool = False
    cleanup_action: str = "keep"
    cleanup_dest: str = ""


def _count_tree(path: Path) -> tuple[int, int]:
    files, total = 0, 0
    if not path.exists():
        return 0, 0
    if path.is_file():
        try:
            return 1, path.stat().st_size
        except OSError:
            return 1, 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in (".git",)]
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
                files += 1
            except OSError:
                files += 1
    return files, total


def _router_references(rel: str) -> bool:
    needle = rel.replace("\\", "/").split("/")[-1]
    for py in (REPO / "router").rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if rel in text or needle in text:
            return True
    for sh in (REPO / "scripts").glob("*.sh"):
        try:
            if rel in sh.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            pass
    return False


def classify(rel: str, *, is_dir: bool) -> tuple[str, str, str]:
    p = rel.replace("\\", "/").rstrip("/")
    name = Path(p).name

    if any(p.startswith(k) or p == k.rstrip("/") for k in KEEP_PREFIXES):
        if p.startswith("scripts/") and p.endswith(".py"):
            return "project_script", "project python script", "keep"
        return "project_source", "core project path", "keep"

    if p == "docs/PROJECT_STRUCTURE.md" or p.startswith("docs/reports/") and name.endswith(".md"):
        if name != "FILE_TREE.full.md":
            return "project_doc", "audit report markdown", "keep"
        return "generated_artifact", "full file tree dump", "move_archive"

    if p.startswith("scripts/") and (name.endswith(".py") or name.endswith(".sh")):
        return "project_script", "executable script", "keep"

    if "node_modules" in p:
        return "vendor_dependency", "nested node_modules", "move_vendor"

    if name in VENDOR_MARKERS or any(f"/{m}/" in f"/{p}/" or p.endswith(f"/{m}") for m in VENDOR_MARKERS):
        if p.startswith("tmp/") or p == "tmp":
            return "runtime_artifact", "runtime cache/log", "move_runtime"
        return "vendor_dependency", f"vendor marker: {name}", "move_vendor"

    if p.startswith("tmp/") or p == "tmp":
        return "runtime_artifact", "runtime data", "move_runtime"

    if p.endswith(".ndjson") or p.endswith(".log") or p.endswith(".trace"):
        return "runtime_artifact", "runtime log/trace", "move_runtime"

    if name in LOCK_FILES and p != f"ui/{name}":
        if "pdf-export" in p:
            return "foreign_project", "nested lockfile in pdf-export tool", "keep"
        return "unknown_review", "nested lockfile", "review"

    if p.startswith("scripts/pdf-export"):
        if name == "package.json" or name.endswith(".mjs") or name.endswith(".json"):
            return "project_script", "pdf export helper (no router import)", "keep"
        return "foreign_project", "pdf export subtree", "review"

    if (REPO / p).is_dir() and (REPO / p / "package.json").exists() and not p.startswith("ui"):
        return "foreign_project", "nested package.json project", "move_foreign"

    if p.startswith("ui/"):
        return "project_source", "ui package", "keep"

    return "unknown_review", "unclassified", "review"


def scan() -> list[ArtifactEntry]:
    entries: list[ArtifactEntry] = []
    seen: set[str] = set()

    def add(path: Path, force_class: str | None = None) -> None:
        rel = str(path.relative_to(REPO)).replace("\\", "/")
        if rel in seen or rel == ".":
            return
        seen.add(rel)
        fc, n = _count_tree(path)
        cls, reason, action = classify(rel, is_dir=path.is_dir())
        if force_class:
            cls = force_class
        linked = _router_references(rel)
        dest = ""
        if action == "move_vendor":
            dest = f"~/.local/share/ai-runtime/archive/vendor-dumps/{{date}}/{rel.replace('/', '_')}"
        elif action == "move_runtime":
            dest = f"~/.local/share/ai-runtime/{{captures|cache|benchmarks|archive/runtime-tmp}}"
        elif action == "move_archive":
            dest = "~/.local/share/ai-runtime/archive/file-tree/FILE_TREE.full.md"
        elif action == "move_foreign":
            dest = f"~/.local/share/ai-runtime/archive/foreign-projects/{{date}}/{rel}"

        entries.append(
            ArtifactEntry(
                path=rel,
                classification=cls,
                file_count=fc,
                bytes=n,
                reason=reason,
                linked_to_project=linked,
                cleanup_action=action,
                cleanup_dest=dest,
            )
        )

    for rel in PRIORITY_PATHS:
        p = REPO / rel
        if p.exists():
            add(p)

    # top-level scan
    for child in sorted(REPO.iterdir()):
        if child.name.startswith(".") and child.name not in {".github"}:
            if child.name in {".venv-llamaindex", ".git"}:
                add(child)
            continue
        if child.is_dir():
            add(child)
        elif child.suffix in {".md", ".yml", ".yaml", ".json", ".sh"}:
            add(child)

    # nested node_modules anywhere
    for nm in REPO.rglob("node_modules"):
        if "__pycache__" in nm.parts:
            continue
        add(nm, force_class="vendor_dependency")

    # nested lockfiles
    for pattern in LOCK_FILES:
        for lf in REPO.rglob(pattern):
            if "node_modules" in lf.parts:
                continue
            add(lf.parent if lf.name == pattern and pattern != "package.json" else lf)

    return entries


def render_md(entries: list[ArtifactEntry]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    by_class: dict[str, list[ArtifactEntry]] = {}
    for e in entries:
        by_class.setdefault(e.classification, []).append(e)

    lines = [
        "# Foreign Artifacts Audit",
        "",
        f"> Generated: {ts}",
        f"> Root: `{REPO}`",
        "",
        "## Summary",
        "",
        "| Class | Paths | Files | Size (MB) |",
        "|-------|------:|------:|----------:|",
    ]
    for cls in CLASSIFICATIONS:
        rows = by_class.get(cls, [])
        if not rows:
            continue
        files = sum(r.file_count for r in rows)
        size = sum(r.bytes for r in rows) / 1024 / 1024
        lines.append(f"| {cls} | {len(rows)} | {files:,} | {size:.1f} |")

    lines += ["", "## Priority targets", "", "| Path | Class | Files | MB | Action | Linked |", "|------|-------|------:|---:|--------|--------|"]
    for e in sorted(entries, key=lambda x: -x.bytes):
        if e.path in PRIORITY_PATHS or "node_modules" in e.path or e.classification in (
            "vendor_dependency", "foreign_project", "runtime_artifact", "generated_artifact",
        ):
            lines.append(
                f"| `{e.path}` | {e.classification} | {e.file_count:,} | {e.bytes/1024/1024:.1f} | "
                f"{e.cleanup_action} | {e.linked_to_project} |"
            )

    lines += [
        "",
        "## Cleanup recommendations",
        "",
        "- `scripts/pdf-export/node_modules` → **move** to `archive/vendor-dumps/` (keep `.mjs` + `package.json`)",
        "- `.venv-llamaindex` → **move** to `archive/vendor-dumps/`",
        "- `tmp/*` → **move** to `AI_RUNTIME_DATA_DIR` (repo keeps `tmp/.gitkeep` only)",
        "- `docs/reports/FILE_TREE.full.md` → **move** to `archive/file-tree/`",
        "",
        "*Regenerate: `python3 scripts/audit-foreign-artifacts.py`*",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    entries = scan()
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entries": [asdict(e) for e in entries],
        "summary": dict(Counter(e.classification for e in entries)),
    }
    reports_dir().mkdir(parents=True, exist_ok=True)
    md_path = reports_dir() / "foreign-artifacts-audit.md"
    json_path = audit_json_dir() / "foreign-artifacts-audit.json"
    md_path.write_text(render_md(entries), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Written: {md_path}")
    print(f"Written: {json_path}")
    print(f"Entries: {len(entries)} · vendor: {payload['summary'].get('vendor_dependency', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
