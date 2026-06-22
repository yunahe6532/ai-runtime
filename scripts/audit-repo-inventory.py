#!/usr/bin/env python3
"""Repo inventory audit — classify files without deleting anything."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from runtime_kernel.project_index import PathClass, classify_path  # noqa: E402
from runtime_kernel.runtime_paths import audit_json_dir, repo_root, reports_dir  # noqa: E402

REPO = repo_root()

INVENTORY_CLASSES = (
    "source", "docs", "config", "tests", "scripts",
    "runtime_logs", "captures", "benchmark_outputs", "generated_docs",
    "vendor", "venv", "node_modules", "cache", "git_metadata", "unknown",
)

SKIP_WALK_PREFIXES = {
    ".git/objects", ".git/modules", "node_modules", ".venv-llamaindex",
}


def _map_path_class(pc: PathClass, relpath: str) -> str:
    p = relpath.replace("\\", "/")
    if pc == PathClass.GIT_METADATA:
        return "git_metadata"
    if pc in (PathClass.VENDOR,) or "node_modules" in p:
        return "node_modules" if "node_modules" in p else "vendor"
    if pc == PathClass.CACHE or "__pycache__" in p or ".pytest_cache" in p:
        return "cache"
    if ".venv" in p:
        return "venv"
    if pc == PathClass.RUNTIME_DATA or p.startswith("tmp/") or "/captures/" in f"/{p}/":
        if "benchmark" in p:
            return "benchmark_outputs"
        if p.endswith((".log", ".trace")) or "/captures/" in f"/{p}/":
            return "captures"
        return "runtime_logs"
    if pc == PathClass.GENERATED or "FILE_TREE" in p:
        return "generated_docs"
    if pc == PathClass.DOC:
        return "docs"
    if pc == PathClass.CONFIG:
        return "config"
    if pc == PathClass.TEST:
        return "tests"
    if pc == PathClass.SCRIPT:
        return "scripts"
    if pc == PathClass.SOURCE:
        return "source"
    return "unknown"


def _git_file_states() -> dict[str, str]:
    states: dict[str, str] = {}
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-z", "--others", "-i", "--exclude-standard"],
            capture_output=True,
            check=False,
        )
        if r.returncode == 0:
            for p in r.stdout.decode("utf-8", errors="replace").split("\0"):
                if p:
                    states[p] = "ignored"
        r2 = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-z"],
            capture_output=True,
            check=False,
        )
        if r2.returncode == 0:
            for p in r2.stdout.decode("utf-8", errors="replace").split("\0"):
                if p:
                    states[p] = "tracked"
        r3 = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-z", "--others", "--exclude-standard"],
            capture_output=True,
            check=False,
        )
        if r3.returncode == 0:
            for p in r3.stdout.decode("utf-8", errors="replace").split("\0"):
                if p:
                    if p not in states:
                        states[p] = "untracked"
    except OSError:
        pass
    return states


def _should_skip_walk(rel_dir: str) -> bool:
    rd = rel_dir.replace("\\", "/")
    for prefix in SKIP_WALK_PREFIXES:
        if rd == prefix or rd.startswith(prefix + "/"):
            return True
    top = rd.split("/")[0] if rd != "." else ""
    if top in {"node_modules", ".venv-llamaindex"}:
        return True
    return False


@dataclass
class InventoryStats:
    total_files: int = 0
    total_dirs: int = 0
    total_bytes: int = 0
    by_class: dict[str, dict[str, int]] = field(default_factory=dict)
    by_extension: dict[str, dict[str, int]] = field(default_factory=dict)
    top_level: dict[str, dict[str, int]] = field(default_factory=dict)
    git_states: dict[str, int] = field(default_factory=dict)
    mtime_buckets: dict[str, int] = field(default_factory=dict)
    large_files: list[dict[str, object]] = field(default_factory=list)
    generated_candidates: list[dict[str, object]] = field(default_factory=list)
    index_include: list[str] = field(default_factory=list)
    index_exclude: list[str] = field(default_factory=list)


def scan_repo() -> InventoryStats:
    stats = InventoryStats()
    git_states = _git_file_states()
    now = datetime.now(timezone.utc).timestamp()
    ext_counter: Counter[str] = Counter()
    ext_bytes: Counter[str] = Counter()
    class_files: Counter[str] = Counter()
    class_bytes: Counter[str] = Counter()
    top_files: Counter[str] = Counter()
    top_bytes: Counter[str] = Counter()
    mtime_buckets: Counter[str] = Counter()

    for dirpath, dirnames, filenames in os.walk(REPO):
        rel_dir = str(Path(dirpath).relative_to(REPO))
        if _should_skip_walk(rel_dir):
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if not _should_skip_walk(
            f"{rel_dir}/{d}" if rel_dir != "." else d
        )]
        if rel_dir != ".":
            stats.total_dirs += 1

        for name in filenames:
            p = Path(dirpath) / name
            rel = str(p.relative_to(REPO)).replace("\\", "/")
            try:
                st = p.stat()
            except OSError:
                continue
            size = int(st.st_size)
            stats.total_files += 1
            stats.total_bytes += size

            pc = classify_path(rel)
            bucket = _map_path_class(pc, rel)
            class_files[bucket] += 1
            class_bytes[bucket] += size

            ext = p.suffix.lower() or "(noext)"
            ext_counter[ext] += 1
            ext_bytes[ext] += size

            top = rel.split("/")[0] if "/" in rel else "(root)"
            top_files[top] += 1
            top_bytes[top] += size

            age_days = (now - st.st_mtime) / 86400
            if age_days <= 7:
                mtime_buckets["7d"] += 1
            elif age_days <= 30:
                mtime_buckets["30d"] += 1
            elif age_days <= 90:
                mtime_buckets["90d"] += 1
            else:
                mtime_buckets["older"] += 1

            gs = git_states.get(rel, "unknown")
            stats.git_states[gs] = stats.git_states.get(gs, 0) + 1

            if size >= 500_000:
                stats.large_files.append({"path": rel, "bytes": size, "class": bucket})
            if bucket in ("generated_docs", "runtime_logs", "cache", "vendor", "node_modules", "venv"):
                if len(stats.generated_candidates) < 200:
                    stats.generated_candidates.append({"path": rel, "class": bucket, "bytes": size})

            from runtime_kernel.project_index import path_included_in_index
            if path_included_in_index(rel):
                if len(stats.index_include) < 50:
                    stats.index_include.append(rel)
            else:
                if bucket not in ("unknown", "source") and len(stats.index_exclude) < 80:
                    stats.index_exclude.append(f"{rel} ({bucket})")

    stats.large_files.sort(key=lambda x: int(x["bytes"]), reverse=True)
    stats.large_files = stats.large_files[:40]
    stats.by_class = {
        k: {"files": class_files[k], "bytes": class_bytes[k]}
        for k in sorted(class_files.keys())
    }
    stats.by_extension = {
        k: {"files": ext_counter[k], "bytes": ext_bytes[k]}
        for k in sorted(ext_counter.keys(), key=lambda x: (-ext_counter[x], x))
    }
    stats.top_level = {
        k: {"files": top_files[k], "bytes": top_bytes[k]}
        for k in sorted(top_files.keys(), key=lambda x: (-top_files[x], x))
    }
    stats.mtime_buckets = dict(mtime_buckets)
    return stats


def render_md(stats: InventoryStats) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# Repo Inventory Audit",
        "",
        f"> Generated: {ts}",
        f"> Root: `{REPO}`",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|--------|------:|",
        f"| Files | {stats.total_files:,} |",
        f"| Dirs (walked) | {stats.total_dirs:,} |",
        f"| Total size | {stats.total_bytes / 1024 / 1024:.1f} MB |",
        "",
        "## Classification",
        "",
        "| Class | Files | Size (MB) |",
        "|-------|------:|----------:|",
    ]
    for cls, row in sorted(stats.by_class.items(), key=lambda x: -x[1]["files"]):
        lines.append(f"| {cls} | {row['files']:,} | {row['bytes'] / 1024 / 1024:.1f} |")

    lines += ["", "## Top-level directories", "", "| Dir | Files | Size (MB) |", "|-----|------:|----------:|"]
    for name, row in sorted(stats.top_level.items(), key=lambda x: -x[1]["files"])[:20]:
        lines.append(f"| `{name}` | {row['files']:,} | {row['bytes'] / 1024 / 1024:.1f} |")

    lines += ["", "## Git state", ""]
    for k, v in sorted(stats.git_states.items()):
        lines.append(f"- **{k}**: {v:,}")

    lines += ["", "## Mtime distribution", ""]
    for k, v in stats.mtime_buckets.items():
        lines.append(f"- {k}: {v:,}")

    lines += ["", "## Large files (≥500KB)", "", "| Path | MB | Class |", "|------|---:|-------|"]
    for item in stats.large_files[:25]:
        lines.append(f"| `{item['path']}` | {int(item['bytes']) / 1024 / 1024:.1f} | {item['class']} |")

    lines += ["", "## Project Index policy", "", "### Include samples", ""]
    for p in stats.index_include[:20]:
        lines.append(f"- `{p}`")
    lines += ["", "### Exclude samples", ""]
    for p in stats.index_exclude[:25]:
        lines.append(f"- `{p}`")

    lines += [
        "",
        "## Runtime storage",
        "",
        "Runtime artifacts should live under `AI_RUNTIME_DATA_DIR` (default `~/.local/share/ai-runtime`).",
        "Repo `tmp/` is fallback only.",
        "",
        "*Regenerate: `python3 scripts/audit-repo-inventory.py`*",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    stats = scan_repo()
    md = render_md(stats)
    reports_dir().mkdir(parents=True, exist_ok=True)
    md_path = reports_dir() / "repo-inventory.md"
    json_path = audit_json_dir() / "repo-inventory.json"
    md_path.write_text(md, encoding="utf-8")
    payload = asdict(stats)
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload["repo_root"] = str(REPO)
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Written: {md_path}")
    print(f"Written: {json_path}")
    print(f"Files: {stats.total_files:,} · Classes: {len(stats.by_class)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
