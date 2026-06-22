#!/usr/bin/env python3
"""Archive unreachable legacy optimizer modules (no delete)."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from runtime_kernel.runtime_paths import archive_dir, audit_json_dir, reports_dir, repo_root  # noqa: E402

REPO = repo_root()
ROUTER_LEGACY = REPO / "router" / "legacy"
ARCHIVE_DAY = "20260622"
ARCHIVE_TARGETS = (
    ("context_optimizer.py", "context_optimizer"),
    ("runtime_optimizer.py", "runtime_optimizer"),
)

STUB_TEMPLATE = '''"""ARCHIVED {day} — see docs/reports/legacy-archive-applied.md.

Original: {archive_path}
Replacement: dynamic_context_scheduler + runtime_core.indexing_helpers
"""

_ARCHIVED = True
_ARCHIVE_PATH = "{archive_path}"


def __getattr__(name: str):
    raise ImportError(
        "legacy.{module} archived; see docs/reports/legacy-archive-applied.md "
        f"({{_ARCHIVE_PATH}})"
    )
'''


@dataclass
class ArchiveRecord:
    source: str
    destination: str
    stub: str
    status: str = "planned"
    error: str = ""


def _plan(day: str) -> tuple[Path, list[ArchiveRecord]]:
    dest_root = archive_dir("deprecated", day=day) / "legacy"
    records: list[ArchiveRecord] = []
    for filename, module in ARCHIVE_TARGETS:
        src = ROUTER_LEGACY / filename
        dest = dest_root / filename
        records.append(
            ArchiveRecord(
                source=str(src.relative_to(REPO)),
                destination=str(dest),
                stub=str(src.relative_to(REPO)),
            )
        )
    return dest_root, records


def apply(day: str, *, dry_run: bool) -> list[ArchiveRecord]:
    dest_root, records = _plan(day)
    for rec in records:
        src = REPO / rec.source
        dest = Path(rec.destination)
        if not src.is_file():
            rec.status = "missing"
            rec.error = "source not found"
            continue
        if dry_run:
            rec.status = "planned"
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                rec.status = "skipped"
                rec.error = "destination exists"
                continue
            shutil.copy2(src, dest)
            stub_path = REPO / rec.stub
            stub_path.write_text(
                STUB_TEMPLATE.format(
                    day=day,
                    archive_path=dest,
                    module=src.stem,
                ),
                encoding="utf-8",
            )
            rec.status = "archived"
        except OSError as exc:
            rec.status = "error"
            rec.error = str(exc)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", default=ARCHIVE_DAY, help="Archive stamp YYYYMMDD")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and not args.apply:
        parser.error("pass --dry-run or --apply")
    records = apply(args.day, dry_run=args.dry_run)
    log = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "day": args.day,
        "dry_run": args.dry_run,
        "records": [asdict(r) for r in records],
    }
    audit_json_dir().mkdir(parents=True, exist_ok=True)
    out = audit_json_dir() / "legacy-archive-applied.json"
    out.write_text(json.dumps(log, indent=2), encoding="utf-8")
    for rec in records:
        print(f"{rec.status:8} {rec.source} -> {rec.destination}")
        if rec.error:
            print(f"         {rec.error}")
    print(f"Written: {out}")
    return 0 if all(r.status in ("planned", "archived", "skipped") for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
