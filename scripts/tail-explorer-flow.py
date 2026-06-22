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

from explorer_trace import format_flow_event  # noqa: E402


def _follow(path: Path, from_start: bool) -> None:
    last_plan_key = ""
    while not path.is_file():
        time.sleep(0.2)
    with path.open(encoding="utf-8") as fh:
        if not from_start:
            fh.seek(0, 2)
        while True:
            line = fh.readline()
            if not line:
                time.sleep(0.15)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Tail explorer trace as human-readable flow")
    ap.add_argument(
        "path",
        nargs="?",
        default=str(ROOT / "tmp/cursor-captures/explorer-trace.ndjson"),
        help="NDJSON trace file",
    )
    ap.add_argument(
        "--from-start",
        action="store_true",
        help="Replay entire file then follow (default: tail -f from end)",
    )
    args = ap.parse_args()
    _follow(Path(args.path), from_start=args.from_start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
