#!/usr/bin/env python3
"""Aggregate planner promotion metrics from explorer trace NDJSON."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from explorer_trace import default_trace_path  # noqa: E402
from runtime_kernel.runtime_paths import audit_json_dir, reports_dir  # noqa: E402

PROMOTION_EVENTS = frozenset({
    "planner.promotion.evaluated",
    "planner.promotion.eligible",
    "planner.promotion.blocked",
    "planner.promotion.applied",
    "planner.promotion.skipped",
})


def _load_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return rows


def aggregate(rows: list[dict]) -> dict:
    promo_rows = [r for r in rows if str(r.get("event") or "") in PROMOTION_EVENTS]
    by_event = Counter(str(r.get("event") or "") for r in promo_rows)

    eligible = sum(1 for r in promo_rows if r.get("event") == "planner.promotion.eligible")
    evaluated = sum(1 for r in promo_rows if r.get("event") == "planner.promotion.evaluated")
    applied = sum(1 for r in promo_rows if r.get("event") == "planner.promotion.applied")
    blocked_eval = sum(1 for r in promo_rows if r.get("event") == "planner.promotion.blocked")
    skipped = sum(1 for r in promo_rows if r.get("event") == "planner.promotion.skipped")

    skip_reasons: Counter[str] = Counter()
    block_reasons: Counter[str] = Counter()
    apply_actions: Counter[str] = Counter()
    intent_applied: Counter[str] = Counter()
    intent_eligible: Counter[str] = Counter()

    for r in promo_rows:
        ev = str(r.get("event") or "")
        intent = str(r.get("router_intent") or r.get("intent") or "unknown")
        if ev == "planner.promotion.skipped":
            skip_reasons[str(r.get("reason") or "unknown")] += 1
        if ev == "planner.promotion.blocked":
            reason = str(r.get("reason") or "")
            if r.get("blocked_reasons"):
                for b in r.get("blocked_reasons") or []:
                    block_reasons[str(b)] += 1
            elif reason:
                block_reasons[reason] += 1
        if ev == "planner.promotion.applied":
            act = str(r.get("effective_action") or r.get("allowed_action") or "unknown")
            apply_actions[act] += 1
            intent_applied[intent] += 1
        if ev == "planner.promotion.eligible":
            intent_eligible[intent] += 1

    # dedupe: eligible event often pairs with evaluated — use evaluated eligible flag
    eligible_flags = sum(
        1 for r in promo_rows
        if r.get("event") == "planner.promotion.evaluated" and r.get("eligible") is True
    )
    if eligible_flags:
        eligible = eligible_flags

    n_eval = max(evaluated, 1)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_rows_total": len(rows),
        "promotion_events_total": len(promo_rows),
        "counts": {
            "evaluated": evaluated,
            "eligible": eligible,
            "applied": applied,
            "blocked_events": blocked_eval,
            "skipped": skipped,
        },
        "rates": {
            "eligible_rate": round(eligible / n_eval, 4),
            "applied_rate": round(applied / n_eval, 4),
            "blocked_rate": round(blocked_eval / n_eval, 4),
            "skipped_rate": round(skipped / n_eval, 4),
            "apply_of_eligible": round(applied / max(eligible, 1), 4),
        },
        "by_event": dict(by_event),
        "skip_reasons": dict(skip_reasons.most_common()),
        "block_reasons": dict(block_reasons.most_common(20)),
        "apply_actions": dict(apply_actions),
        "intent_eligible": dict(intent_eligible),
        "intent_applied": dict(intent_applied),
    }


def render_md(summary: dict, *, trace_path: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    c = summary.get("counts") or {}
    r = summary.get("rates") or {}
    lines = [
        "# Planner Promotion Metrics",
        "",
        f"> Generated: {ts}",
        "",
        f"Trace: `{trace_path}`",
        "",
        "## Summary",
        "",
        "| Metric | Count | Rate (of evaluated) |",
        "|--------|------:|--------------------:|",
        f"| evaluated | {c.get('evaluated', 0)} | — |",
        f"| eligible | {c.get('eligible', 0)} | {r.get('eligible_rate', 0):.1%} |",
        f"| applied | {c.get('applied', 0)} | {r.get('applied_rate', 0):.1%} |",
        f"| blocked (events) | {c.get('blocked_events', 0)} | {r.get('blocked_rate', 0):.1%} |",
        f"| skipped | {c.get('skipped', 0)} | {r.get('skipped_rate', 0):.1%} |",
        "",
        f"**apply / eligible:** {r.get('apply_of_eligible', 0):.1%}",
        "",
        "## Skip reasons",
        "",
    ]
    for reason, n in (summary.get("skip_reasons") or {}).items():
        lines.append(f"- `{reason}`: {n}")
    if not summary.get("skip_reasons"):
        lines.append("- (none)")
    lines += ["", "## Block reasons (top)", ""]
    for reason, n in list((summary.get("block_reasons") or {}).items())[:12]:
        lines.append(f"- `{reason}`: {n}")
    if not summary.get("block_reasons"):
        lines.append("- (none)")
    lines += ["", "## Applied actions", ""]
    for act, n in (summary.get("apply_actions") or {}).items():
        lines.append(f"- `{act}`: {n}")
    if not summary.get("apply_actions"):
        lines.append("- (none)")
    lines += ["", "## Intent breakdown", "", "| intent | eligible | applied |", "|--------|--------:|--------:|"]
    intents = sorted(set(list((summary.get("intent_eligible") or {}).keys()) + list((summary.get("intent_applied") or {}).keys())))
    for intent in intents:
        el = (summary.get("intent_eligible") or {}).get(intent, 0)
        ap = (summary.get("intent_applied") or {}).get(intent, 0)
        lines.append(f"| `{intent}` | {el} | {ap} |")
    if not intents:
        lines.append("| — | 0 | 0 |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, default=None, help="NDJSON trace path")
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--md-out", type=Path, default=None)
    args = parser.parse_args()

    trace_path = args.trace or default_trace_path()
    rows = _load_rows(Path(trace_path))
    summary = aggregate(rows)

    json_out = args.json_out or audit_json_dir() / "planner-promotion-metrics.json"
    md_out = args.md_out or reports_dir() / "planner-promotion-metrics.md"
    json_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    md_out.write_text(render_md(summary, trace_path=str(trace_path)), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"\nWritten: {json_out}")
    print(f"Written: {md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
