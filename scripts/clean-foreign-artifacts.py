#!/usr/bin/env python3
"""Move foreign/vendor/runtime artifacts out of repo (quarantine first, no hot path change)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from runtime_kernel.runtime_paths import (  # noqa: E402
    archive_dir,
    audit_json_dir,
    benchmarks_dir,
    captures_dir,
    context_cache_dir,
    repo_root,
    reports_dir,
    runtime_data_dir,
)

REPO = repo_root()
AUDIT_JSON = audit_json_dir() / "foreign-artifacts-audit.json"
LOG_JSON = audit_json_dir() / "foreign-artifacts-cleanup.json"
REPORT_MD = reports_dir() / "foreign-artifacts-cleanup.md"

RECENT_DAYS = 7


@dataclass
class MoveRecord:
    source: str
    destination: str
    action: str
    file_count: int = 0
    bytes: int = 0
    status: str = "planned"
    error: str = ""


def _unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem = dest.name
    parent = dest.parent
    for i in range(1, 1000):
        alt = parent / f"{stem}__{i}"
        if not alt.exists():
            return alt
    return parent / f"{stem}__{int(time.time())}"


def _count(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    if path.is_file():
        try:
            return 1, path.stat().st_size
        except OSError:
            return 1, 0
    files, total = 0, 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            files += 1
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                pass
    return files, total


def _is_recent(path: Path, days: int = RECENT_DAYS) -> bool:
    try:
        age = (time.time() - path.stat().st_mtime) / 86400
        return age <= days
    except OSError:
        return False


def _plan_moves() -> list[MoveRecord]:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    plans: list[MoveRecord] = []

    def plan(source_rel: str, dest: Path, action: str) -> None:
        src = REPO / source_rel
        if not src.exists():
            return
        fc, sz = _count(src)
        plans.append(
            MoveRecord(
                source=source_rel,
                destination=str(dest),
                action=action,
                file_count=fc,
                bytes=sz,
            )
        )

    # vendor — pdf-export node_modules (mandatory)
    plan(
        "scripts/pdf-export/node_modules",
        archive_dir("vendor-dumps") / f"scripts-pdf-export-node_modules",
        "move_vendor",
    )

    # venv
    plan(".venv-llamaindex", archive_dir("vendor-dumps") / "venv-llamaindex", "move_vendor")

    # ui node_modules if present
    if (REPO / "ui/node_modules").exists():
        plan("ui/node_modules", archive_dir("vendor-dumps") / "ui-node_modules", "move_vendor")

    # generated file tree
    plan(
        "docs/reports/FILE_TREE.full.md",
        archive_dir("file-tree") / "FILE_TREE.full.md",
        "move_generated",
    )

    # tmp subtree moves
    tmp = REPO / "tmp"
    if tmp.exists():
        for sub in ("cursor-captures", "context-cache"):
            src = tmp / sub
            if not src.exists():
                continue
            if sub == "cursor-captures":
                dest_base = runtime_data_dir() / "captures"
            else:
                dest_base = runtime_data_dir() / "cache" / "context-cache"
            fc, sz = _count(src)
            plans.append(
                MoveRecord(
                    source=f"tmp/{sub}",
                    destination=str(dest_base),
                    action="move_runtime_merge",
                    file_count=fc,
                    bytes=sz,
                )
            )

        # other tmp files (json audits, etc.)
        for item in tmp.iterdir():
            rel = f"tmp/{item.name}"
            if item.name in {".gitkeep", "cursor-captures", "context-cache"}:
                continue
            if item.is_dir():
                dest = archive_dir("runtime-tmp") / item.name
                plan(rel, dest, "move_runtime_archive")
            elif item.is_file():
                if item.name.endswith((".json", ".ndjson")):
                    dest = benchmarks_dir() / item.name
                else:
                    dest = archive_dir("runtime-tmp") / item.name
                plan(rel, dest, "move_runtime_file")

    return plans


def _merge_tree(src: Path, dest: Path, dry_run: bool) -> str:
    """Merge directory contents into dest. Returns status suffix."""
    if not src.exists():
        return "skipped_missing"
    if dry_run:
        return "dry_run_merge"
    dest.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for item in src.rglob("*"):
        if item.is_dir():
            continue
        rel = item.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target = _unique_dest(target)
        try:
            shutil.move(str(item), str(target))
        except PermissionError:
            try:
                shutil.copy2(item, target)
            except OSError as exc:
                errors.append(str(exc))
    # Docker-created root-owned files: copy done; clear via docker if rmtree fails
    try:
        shutil.rmtree(src)
    except PermissionError:
        subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{src.resolve()}:/src",
                "alpine", "sh", "-c", "rm -rf /src/* /src/.[!.]* 2>/dev/null; rmdir /src 2>/dev/null; true",
            ],
            check=False,
            capture_output=True,
        )
        if src.exists():
            try:
                src.rmdir()
            except OSError:
                pass
    if errors:
        return f"merged_partial:{len(errors)}"
    return "merged"


def _move_path(src: Path, dest: Path, dry_run: bool, delete_vendor: bool) -> str:
    if not src.exists():
        return "skipped_missing"
    if dry_run:
        return "dry_run"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir() and delete_vendor and src.name in {"node_modules", ".venv-llamaindex"}:
        # optional fast path: only when explicitly requested after move failed
        pass
    final_dest = _unique_dest(dest)
    try:
        shutil.move(str(src), str(final_dest))
        return "moved"
    except OSError as exc:
        return f"error:{exc}"


def apply_plans(
    plans: list[MoveRecord],
    *,
    dry_run: bool,
    delete_vendor: bool,
) -> list[MoveRecord]:
    results: list[MoveRecord] = []
    for rec in plans:
        src = REPO / rec.source
        dest = Path(rec.destination).expanduser()
        out = MoveRecord(**asdict(rec))
        try:
            if rec.action == "move_runtime_merge":
                if dry_run:
                    out.status = "dry_run_merge"
                else:
                    out.status = _merge_tree(src, dest, dry_run=False)
            elif src.is_file():
                out.status = _move_path(src, dest, dry_run, delete_vendor)
            elif src.is_dir():
                out.status = _move_path(src, dest, dry_run, delete_vendor)
            else:
                out.status = "skipped_missing"
        except OSError as exc:
            out.status = "error"
            out.error = str(exc)
        results.append(out)

    # ensure tmp/.gitkeep
    if not dry_run:
        keep = REPO / "tmp" / ".gitkeep"
        keep.parent.mkdir(parents=True, exist_ok=True)
        if not keep.exists():
            keep.write_text("", encoding="utf-8")

    # delete vendor with explicit flag (after move attempt for empty dirs)
    if delete_vendor and not dry_run:
        for vendor in (
            REPO / "scripts/pdf-export/node_modules",
            REPO / ".venv-llamaindex",
            REPO / "ui/node_modules",
        ):
            if vendor.exists():
                shutil.rmtree(vendor, ignore_errors=True)

    return results


def render_report(results: list[MoveRecord], *, dry_run: bool) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    mode = "dry-run" if dry_run else "apply"
    lines = [
        "# Foreign Artifacts Cleanup",
        "",
        f"> Generated: {ts}",
        f"> Mode: **{mode}**",
        f"> Data dir: `{runtime_data_dir()}`",
        "",
        "| Source | Destination | Action | Files | MB | Status |",
        "|--------|-------------|--------|------:|---:|--------|",
    ]
    for r in results:
        dest = r.destination if len(r.destination) <= 80 else r.destination[:77] + "..."
        lines.append(
            f"| `{r.source}` | `{dest}` | {r.action} | {r.file_count:,} | "
            f"{r.bytes/1024/1024:.1f} | {r.status} |"
        )
    errors = [r for r in results if r.status.startswith("error") or r.error]
    if errors:
        lines += ["", "## Errors", ""]
        for r in errors:
            lines.append(f"- `{r.source}`: {r.status} {r.error}")
    lines += ["", "*Re-run audit after cleanup: `python3 scripts/audit-foreign-artifacts.py`*", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean foreign/vendor/runtime artifacts from repo")
    parser.add_argument("--dry-run", action="store_true", help="Preview moves only")
    parser.add_argument("--apply", action="store_true", help="Execute moves")
    parser.add_argument("--delete-vendor", action="store_true", help="Remove vendor dirs after move (rmtree)")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        args.dry_run = True

    plans = _plan_moves()
    results = apply_plans(plans, dry_run=args.dry_run, delete_vendor=args.delete_vendor)

    reports_dir().mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text(render_report(results, dry_run=args.dry_run), encoding="utf-8")
    LOG_JSON.write_text(
        json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dry_run": args.dry_run,
            "delete_vendor": args.delete_vendor,
            "results": [asdict(r) for r in results],
        }, indent=2),
        encoding="utf-8",
    )
    moved = sum(1 for r in results if r.status in ("moved", "merged"))
    print(f"Written: {REPORT_MD}")
    print(f"Written: {LOG_JSON}")
    print(f"Planned: {len(results)} · completed: {moved} · mode: {'dry-run' if args.dry_run else 'apply'}")
    for r in results:
        print(f"  {r.status:12} {r.source} -> {r.destination} ({r.bytes/1024/1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
