#!/usr/bin/env python3
"""Live runtime viewer — Cursor-style thinking / tool / result stream.

Default (실시간 감시):
  python3 scripts/tail-explorer-flow.py
  python3 scripts/tail-explorer-flow.py --follow

처음부터 재생 + 실시간 이어가기:
  python3 scripts/tail-explorer-flow.py --from-start --follow

개발자용 verbose:
  python3 scripts/tail-explorer-flow.py --verbose --follow
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from explorer_trace import (  # noqa: E402
    build_flow_graph,
    default_trace_path,
    diagnose_trace_file,
    format_flow_event,
    format_live_summary,
    replay_trace_file,
    should_emit_live_event,
)


def _print_banner(path: Path, *, live: bool, follow: bool, from_start: bool, flow_id: str) -> None:
    mode = "LIVE (Cursor-style)" if live else "VERBOSE (developer)"
    tail = "follow" if follow else "replay"
    start = "from start" if from_start else "tail only (new events)"
    fid = f" · flow_id={flow_id}" if flow_id else ""
    print("═" * 60)
    print(f"  Runtime Explorer · {mode}")
    print(f"  trace: {path}")
    print(f"  mode: {tail} · {start}{fid}")
    print("  Ctrl+C to stop")
    print("═" * 60)
    if follow and not path.is_file():
        print("  (waiting for trace file…)")
        sys.stdout.flush()


def _print_diagnosis(path: Path, flow_id: str = "") -> int:
    diag = diagnose_trace_file(path)
    print(f"trace path: {diag['path']}")
    print(f"status: {diag['status']} — {diag.get('message', '')}")
    if not diag.get("exists"):
        print("hint: router must run with EXPLORER_TRACE_ENABLED=1")
        print(f"hint: default path → {default_trace_path()}")
        print("hint: docker → docker compose logs -f cursor-local-llm-router")
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


def _format_row(row: dict, *, live: bool) -> str | None:
    if live:
        return format_live_summary(row)
    return format_flow_event(row)


def _process_row(
    row: dict,
    *,
    live: bool,
    flow_id: str,
    last_plan_key: str,
    last_live_key: str,
) -> tuple[str | None, str, str]:
    if flow_id and str(row.get("flow_id") or row.get("req_id") or "") != flow_id:
        return None, last_plan_key, last_live_key

    event = str(row.get("event") or "")
    if event == "plan":
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
            return None, last_plan_key, last_live_key
        last_plan_key = key

    if live:
        emit, dedupe_key = should_emit_live_event(row, last_key=last_live_key)
        if not emit:
            return None, last_plan_key, last_live_key
        if dedupe_key != last_live_key:
            last_live_key = dedupe_key

    if event == "action_emit" and not live:
        return None, last_plan_key, last_live_key
    if event == "action_emit" and live:
        return None, last_plan_key, last_live_key

    block = _format_row(row, live=live)
    return block, last_plan_key, last_live_key


def _follow(
    path: Path,
    *,
    from_start: bool,
    follow: bool,
    flow_id: str = "",
    live: bool = True,
) -> int:
    _print_banner(path, live=live, follow=follow, from_start=from_start, flow_id=flow_id)

    if not path.is_file():
        if not follow:
            return _print_diagnosis(path, flow_id=flow_id)
        while not path.is_file():
            time.sleep(0.25)

    if from_start and not follow:
        blocks = replay_trace_file(path, from_start=True, flow_id=flow_id)
        if not blocks:
            return _print_diagnosis(path, flow_id=flow_id)
        formatter = format_live_summary if live else format_flow_event
        last_plan_key = ""
        last_live_key = ""
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                block, last_plan_key, last_live_key = _process_row(
                    row,
                    live=live,
                    flow_id=flow_id,
                    last_plan_key=last_plan_key,
                    last_live_key=last_live_key,
                )
                if block:
                    sys.stdout.write(block + "\n\n")
        sys.stdout.flush()
        return 0

    last_plan_key = ""
    last_live_key = ""
    with path.open(encoding="utf-8") as fh:
        if not from_start:
            fh.seek(0, 2)
        while True:
            line = fh.readline()
            if not line:
                if not follow:
                    break
                time.sleep(0.12)
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
            block, last_plan_key, last_live_key = _process_row(
                row,
                live=live,
                flow_id=flow_id,
                last_plan_key=last_plan_key,
                last_live_key=last_live_key,
            )
            if block:
                sys.stdout.write(block + "\n\n")
                sys.stdout.flush()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Live Cursor-style runtime viewer (thinking → tool → result)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 scripts/tail-explorer-flow.py              # live tail (default)\n"
            "  python3 scripts/tail-explorer-flow.py --from-start --follow\n"
            "  python3 scripts/watch-runtime.py                   # same as above\n"
        ),
    )
    ap.add_argument(
        "path",
        nargs="?",
        default=str(default_trace_path()),
        help="NDJSON trace file",
    )
    ap.add_argument(
        "--from-start",
        action="store_true",
        help="Read from beginning, then keep following if --follow",
    )
    ap.add_argument(
        "--follow",
        "-f",
        action="store_true",
        default=True,
        help="Keep tailing for new events (default: on)",
    )
    ap.add_argument(
        "--no-follow",
        action="store_true",
        help="Replay once and exit (disables --follow)",
    )
    ap.add_argument(
        "--flow-id",
        default="",
        help="Filter to a single flow_id / req_id",
    )
    ap.add_argument(
        "--diagnose",
        action="store_true",
        help="Print trace file diagnosis and exit",
    )
    ap.add_argument(
        "--graph",
        action="store_true",
        help="Render ASCII pipeline graph (not live)",
    )
    ap.add_argument(
        "--live",
        action="store_true",
        default=True,
        help="Cursor-style compact summary (default: on)",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Developer event names (planner.shadow.compared, etc.)",
    )
    args = ap.parse_args()
    path = Path(args.path)
    follow = args.follow and not args.no_follow
    live = args.live and not args.verbose

    if args.diagnose:
        return _print_diagnosis(path, flow_id=args.flow_id)

    if args.graph:
        graph = build_flow_graph(path, flow_id=args.flow_id)
        print(graph)
        return 0 if "Sequence check: PASS" in graph else 1

    return _follow(
        path,
        from_start=args.from_start,
        follow=follow,
        flow_id=args.flow_id,
        live=live,
    )


if __name__ == "__main__":
    raise SystemExit(main())
