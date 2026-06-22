#!/usr/bin/env python3
"""Tail explorer trace as Cursor-like thinking → tool → result flow."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from explorer_trace import (  # noqa: E402
    default_trace_path,
    diagnose_trace_file,
    format_flow_event,
    replay_trace_file,
)


def _print_diagnosis(path: Path, flow_id: str = "") -> int:
    diag = diagnose_trace_file(path)
    print(f"trace path: {diag['path']}")
    print(f"status: {diag['status']} — {diag.get('message', '')}")
    if not diag.get("exists"):
        print("hint: run router with EXPLORER_TRACE_ENABLED=1 and CAPTURE_HOST_DIR mounted")
        print(f"hint: default host path is {default_trace_path()}")
        return 1
    print(f"lines: {diag.get('line_count', 0)} malformed: {diag.get('malformed_lines', 0)}")
    if diag.get("events"):
        top = sorted(diag["events"].items(), key=lambda x: -x[1])[:12]
        print("events: " + ", ".join(f"{k}={v}" for k, v in top))
    if flow_id:
        if flow_id not in (diag.get("flow_ids") or []):
            print(f"warning: no matching flow_id={flow_id!r}")
            print(f"recent flow_ids: {', '.join(diag.get('flow_ids') or [])[-8:]}")
            return 1
    if diag.get("schema_issues"):
        for issue in diag["schema_issues"][:5]:
            print(f"schema: {issue}")
    return 0 if diag.get("status") == "ok" else 1


def _follow(path: Path, *, from_start: bool, follow: bool, flow_id: str = "") -> int:
    if not path.is_file():
        code = _print_diagnosis(path, flow_id=flow_id)
        if not follow:
            return code
        while not path.is_file():
            time.sleep(0.2)

    if from_start and not follow:
        blocks = replay_trace_file(path, from_start=True, flow_id=flow_id)
        if not blocks:
            return _print_diagnosis(path, flow_id=flow_id)
        for block in blocks:
            sys.stdout.write(block + "\n\n")
        sys.stdout.flush()
        return 0

    last_plan_key = ""
    with path.open(encoding="utf-8") as fh:
        if not from_start:
            fh.seek(0, 2)
        while True:
            line = fh.readline()
            if not line:
                if not follow:
                    break
                time.sleep(0.15)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if flow_id and str(row.get("flow_id") or "") != flow_id:
                continue
            if row.get("event") == "plan":
                key = "|".join(
                    [
                        str(row.get("flow_id") or ""),
                        str(row.get("step") or ""),
                        str(row.get("next_tool") or ""),
                        str(row.get("next_sid") or ""),
                        str(row.get("thinking") or "")[:120],
                    ]
                )
                if key == last_plan_key:
                    continue
                last_plan_key = key
            if row.get("event") == "action_emit":
                continue
            block = format_flow_event(row)
            if block:
                sys.stdout.write(block + "\n\n")
                sys.stdout.flush()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Tail explorer trace as human-readable flow")
    ap.add_argument(
        "path",
        nargs="?",
        default=str(default_trace_path()),
        help="NDJSON trace file",
    )
    ap.add_argument(
        "--from-start",
        action="store_true",
        help="Replay from beginning (default: exit after replay unless --follow)",
    )
    ap.add_argument(
        "--follow",
        action="store_true",
        help="Keep tailing after replay (tail -f)",
    )
    ap.add_argument(
        "--flow-id",
        default="",
        help="Filter to a single flow_id",
    )
    ap.add_argument(
        "--diagnose",
        action="store_true",
        help="Print trace file diagnosis and exit",
    )
    args = ap.parse_args()
    path = Path(args.path)

    if args.diagnose:
        return _print_diagnosis(path, flow_id=args.flow_id)

    follow = args.follow or (not args.from_start)
    return _follow(path, from_start=args.from_start, follow=follow, flow_id=args.flow_id)


if __name__ == "__main__":
    raise SystemExit(main())
