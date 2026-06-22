#!/usr/bin/env python3
"""Analyze captured Cursor requests for duplication and bloat."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_summaries(capture_dir: Path) -> list[dict]:
    files = sorted(capture_dir.glob("*.summary.json"))
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"warn: skip {f.name}: {exc}", file=sys.stderr)
    return out


def overlap_ratio(prev: dict, curr: dict) -> dict:
    prev_hashes = {p["hash"] for m in prev.get("messages", []) for p in m.get("paragraph_hashes", [])}
    curr_hashes = {p["hash"] for m in curr.get("messages", []) for p in m.get("paragraph_hashes", [])}
    prev_msg_hashes = {m["hash"] for m in prev.get("messages", [])}
    curr_msg_hashes = {m["hash"] for m in curr.get("messages", [])}

    para_inter = prev_hashes & curr_hashes
    msg_inter = prev_msg_hashes & curr_msg_hashes

    return {
        "prev_id": prev.get("id"),
        "curr_id": curr.get("id"),
        "prev_est_tokens": prev.get("est_tokens", 0),
        "curr_est_tokens": curr.get("est_tokens", 0),
        "delta_est_tokens": curr.get("est_tokens", 0) - prev.get("est_tokens", 0),
        "message_hash_overlap_pct": round(100 * len(msg_inter) / max(1, len(curr_msg_hashes)), 1),
        "paragraph_hash_overlap_pct": round(100 * len(para_inter) / max(1, len(curr_hashes)), 1),
        "shared_messages": len(msg_inter),
        "shared_paragraphs": len(para_inter),
        "curr_messages": len(curr_msg_hashes),
        "curr_paragraphs": len(curr_hashes),
    }


def role_breakdown(summary: dict) -> list[tuple[str, int, int]]:
    rows = []
    for m in summary.get("messages", []):
        rows.append((m.get("role", "?"), m.get("chars", 0), m.get("est_tokens", 0)))
    return rows


def print_single(summary: dict) -> None:
    print(f"\n=== {summary.get('id')} ===")
    print(
        f"est_tokens={summary.get('est_tokens')} chars={summary.get('total_chars')} "
        f"messages={summary.get('message_count')} tools={summary.get('tool_count')} stream={summary.get('stream')}"
    )
    print(f"roles={summary.get('roles')}")

    by_role: dict[str, list[int]] = {}
    for role, chars, est in role_breakdown(summary):
        by_role.setdefault(role, [0, 0])
        by_role[role][0] += chars
        by_role[role][1] += est
    print("role totals:")
    for role, (chars, est) in sorted(by_role.items(), key=lambda x: -x[1][1]):
        pct = 100 * est / max(1, summary.get("est_tokens", 1))
        print(f"  {role:12s} {est:6d} tok ({pct:5.1f}%)  {chars:8d} chars")

    print("top messages:")
    for m in sorted(summary.get("messages", []), key=lambda x: -x.get("chars", 0))[:8]:
        print(
            f"  [{m.get('index'):02d}] {m.get('role'):10s} "
            f"{m.get('est_tokens'):6d} tok  hash={m.get('hash')}  files={m.get('file_refs', [])[:3]}"
        )
        preview = m.get("preview", "")
        if preview:
            print(f"       {preview[:160]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze captured Cursor chat requests")
    parser.add_argument(
        "--dir",
        default="/home/yunahe/ai-runtime/cursor-local-llm/tmp/cursor-captures",
        help="capture directory",
    )
    parser.add_argument("--last", type=int, default=0, help="only analyze last N captures")
    args = parser.parse_args()

    capture_dir = Path(args.dir)
    if not capture_dir.exists():
        print(f"no captures yet: {capture_dir}")
        print("enable CAPTURE_REQUESTS=1 on router, use Cursor a few times, then rerun")
        return 1

    summaries = load_summaries(capture_dir)
    if args.last > 0:
        summaries = summaries[-args.last :]
    if not summaries:
        print(f"no summary files in {capture_dir}")
        return 1

    print(f"found {len(summaries)} capture(s) in {capture_dir}")

    for s in summaries:
        print_single(s)

    if len(summaries) >= 2:
        print("\n=== consecutive overlap ===")
        print(
            "prev_id -> curr_id | curr_tok | delta_tok | msg_overlap% | para_overlap% | shared_msg/curr_msg"
        )
        for prev, curr in zip(summaries, summaries[1:]):
            o = overlap_ratio(prev, curr)
            print(
                f"{o['prev_id']} -> {o['curr_id']} | "
                f"{o['curr_est_tokens']:6d} | {o['delta_est_tokens']:+6d} | "
                f"{o['message_hash_overlap_pct']:6.1f}% | {o['paragraph_hash_overlap_pct']:6.1f}% | "
                f"{o['shared_messages']}/{o['curr_messages']}"
            )

        last = summaries[-1]
        all_para_hashes: dict[str, int] = {}
        for s in summaries:
            for m in s.get("messages", []):
                for p in m.get("paragraph_hashes", []):
                    all_para_hashes[p["hash"]] = all_para_hashes.get(p["hash"], 0) + 1
        repeated = {h: c for h, c in all_para_hashes.items() if c >= 2}
        print(f"\nrepeated paragraph hashes across all captures: {len(repeated)}")
        if repeated:
            top = sorted(repeated.items(), key=lambda x: -x[1])[:10]
            for h, c in top:
                print(f"  hash={h} seen_in_{c}_captures")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
