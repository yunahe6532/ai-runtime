#!/usr/bin/env python3
"""Pretty-print a 3-stage flow trace file (*.flow.json)."""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

CAPTURE_DIR = "/home/yunahe/ai-runtime/cursor-local-llm/tmp/cursor-captures"


def print_stage(stage: dict) -> None:
    num = stage.get("stage", "?")[0]
    label = stage.get("label", stage.get("stage", "?"))
    print(f"\n{'─' * 60}")
    print(f"  STEP {num}  {label}")
    print(f"{'─' * 60}")

    if stage["stage"] == "1_cursor_in":
        print(f"  messages : {stage.get('message_count')}")
        print(f"  tokens   : {stage.get('est_tokens')}")
        print(f"  tools    : {stage.get('tool_count')}")
        print(f"  roles    : {stage.get('roles')}")
        print(f"  last     : {stage.get('last_role')} — {stage.get('last_preview', '')[:80]}")
        print(f"  chain    : {stage.get('msg_line', '')}")

    elif stage["stage"] == "2_router_proxy":
        print(f"  intent   : {stage.get('intent')}  phase={stage.get('phase')}")
        print(f"  backend  : {stage.get('backend')}  ({stage.get('route_reason')})")
        print(f"  compress : {stage.get('raw_tokens')} → {stage.get('pack_tokens')} tokens  (saved {stage.get('saved_pct')}%)")
        print(f"  messages : {stage.get('message_count')}")
        print(f"  tools    : {stage.get('tools')}")
        print(f"  max_tok  : {stage.get('max_tokens')}")
        print(f"  chain    : {stage.get('msg_line', '')}")
        if stage.get("messages"):
            print("  detail:")
            for m in stage["messages"]:
                tc = " +tool_calls" if "T" in str(m.get("preview", "")) else ""
                print(f"    [{m['index']:2d}] {m['role']:<10} {m['chars']:>6}chars  {m['preview'][:60]}")

    elif stage["stage"] == "3_llm_response":
        print(f"  elapsed  : {stage.get('elapsed_sec')}s  phase={stage.get('phase')}")
        print(f"  raw      : tool_calls={stage.get('tool_calls')} {stage.get('tool_names')}")
        print(f"           content={stage.get('content_chars')}chars")
        if stage.get("content_preview"):
            print(f"           preview={stage['content_preview'][:100]}")
        print(f"  sent     : tool_calls={stage.get('processed_tool_calls')} {stage.get('processed_tool_names')}")
        print(f"           content={stage.get('processed_content_chars')}chars")
        if stage.get("processed_content_preview"):
            print(f"           preview={stage['processed_content_preview'][:100]}")


def show_flow(path: str) -> None:
    data = json.load(open(path, encoding="utf-8"))
    print(f"\n{'═' * 60}")
    print(f"  FLOW {data['id']}")
    print(f"  started: {data.get('started_at')}  elapsed: {data.get('elapsed_sec', '?')}s")
    print(f"{'═' * 60}")
    for stage in data.get("stages", []):
        print_stage(stage)
    print(f"\n{'═' * 60}")
    print(f"  FLOW {data['id']} END")
    print(f"{'═' * 60}\n")


def list_flows() -> None:
    files = sorted(glob.glob(f"{CAPTURE_DIR}/*.flow.json"), key=os.path.getmtime, reverse=True)
    if not files:
        print("flow 파일 없음. FLOW_TRACE=1 로 router 재시작 후 요청을 보내세요.")
        return
    print(f"{'파일':<35} {'elapsed':>8}  stages")
    print("-" * 60)
    for f in files[:20]:
        d = json.load(open(f))
        stages = " → ".join(s["stage"][-1] for s in d.get("stages", []))
        print(f"{os.path.basename(f):<35} {d.get('elapsed_sec', '?'):>6}s  {stages}")


def main() -> None:
    parser = argparse.ArgumentParser(description="3-stage flow trace viewer")
    parser.add_argument("flow_file", nargs="?", help="*.flow.json path")
    parser.add_argument("--list", action="store_true", help="List flow files")
    parser.add_argument("--latest", action="store_true", help="Show latest flow")
    args = parser.parse_args()

    if args.list:
        list_flows()
        return

    if args.latest or not args.flow_file:
        files = sorted(glob.glob(f"{CAPTURE_DIR}/*.flow.json"), key=os.path.getmtime, reverse=True)
        if not files:
            print("flow 파일 없음.", file=sys.stderr)
            sys.exit(1)
        path = files[0]
        if args.latest:
            print(f"[latest] {os.path.basename(path)}")
    else:
        path = args.flow_file

    show_flow(path)


if __name__ == "__main__":
    main()
