"""Dedicated explorer / planner / tool trace — chronological NDJSON SSOT."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

LOG = logging.getLogger("router.explorer_trace")

EXPLORER_TRACE_ENABLED = os.getenv("EXPLORER_TRACE_ENABLED", "1") == "1"
EXPLORER_TRACE_FORMAT = os.getenv("EXPLORER_TRACE_FORMAT", "both").strip().lower()
EXPLORER_TRACE_STDOUT = os.getenv("EXPLORER_TRACE_STDOUT", "1") == "1"
EXPLORER_TRACE_PREVIEW_LINES = int(os.getenv("EXPLORER_TRACE_PREVIEW_LINES", "14"))
EXPLORER_TRACE_PREVIEW_CHARS = int(os.getenv("EXPLORER_TRACE_PREVIEW_CHARS", "2400"))

_lock = threading.Lock()
_boot_logged = False
_last_plan_key = ""
_flow_out: TextIO = sys.stderr
_active_trace_path: Path | None = None


def _repo_root() -> Path:
    try:
        from runtime_kernel.runtime_paths import repo_root
        return repo_root()
    except ImportError:
        return Path(__file__).resolve().parents[1]


def default_trace_path() -> Path:
    global _active_trace_path
    if _active_trace_path is not None:
        return _active_trace_path
    env = (os.getenv("EXPLORER_TRACE_PATH") or "").strip()
    if env:
        _active_trace_path = Path(env)
        return _active_trace_path
    try:
        from runtime_kernel.runtime_paths import explorer_trace_file
        p = explorer_trace_file()
    except ImportError:
        p = _repo_root() / "tmp" / "cursor-captures" / "explorer-trace.ndjson"
    if p.exists() and not os.access(p, os.W_OK):
        alt = p.parent / f"explorer-trace-host-{os.getuid()}.ndjson"
        LOG.warning("explorer trace not writable path=%s — using %s", p, alt)
        _active_trace_path = alt
        return alt
    _active_trace_path = p
    return p


EXPLORER_TRACE_PATH = str(default_trace_path())


def _resolve_path() -> Path:
    env = (os.getenv("EXPLORER_TRACE_PATH") or "").strip()
    if env:
        return Path(env)
    return default_trace_path()


def _flow_id(explicit: str = "") -> str:
    if explicit:
        return explicit
    try:
        from adapters.observe import current_run_id

        return str(current_run_id() or "")
    except ImportError:
        return ""


def _req_id(explicit: str = "") -> str:
    if explicit:
        return explicit
    return _flow_id()


def _short_ts(ts: str) -> str:
    if len(ts) >= 19:
        return ts[11:19]
    return ts[:8] if ts else ""


def _tool_label(tool: str) -> str:
    name = (tool or "Tool").replace("Source", "")
    return name or "Tool"


def _action_command(
    tool: str,
    source_id: str = "",
    pattern: str = "",
    glob_pattern: str = "",
    path: str = "",
    tool_args: dict[str, Any] | None = None,
) -> str:
    args = tool_args or {}
    label = _tool_label(tool)
    target = source_id or path or str(args.get("path") or args.get("target") or "")
    parts = [label]
    if target:
        parts.append(target)
    gp = glob_pattern or str(args.get("glob_pattern") or "")
    pat = pattern or str(args.get("pattern") or "")
    if gp:
        parts.append(gp)
    elif pat:
        parts.append(f'"{pat[:100]}"')
    cmd = str(args.get("command") or "")
    if cmd and label.lower() == "shell":
        parts.append(cmd[:80])
    return " ".join(parts)


def _indent_block(text: str, prefix: str = "  ", max_lines: int = 0) -> list[str]:
    lines: list[str] = []
    for i, raw in enumerate(str(text or "").splitlines()):
        if max_lines and i >= max_lines:
            rest = len(str(text or "").splitlines()) - max_lines
            if rest > 0:
                lines.append(f"{prefix}… ({rest} more lines)")
            break
        line = raw.rstrip()
        if line:
            lines.append(f"{prefix}{line}")
    return lines


def _preview_lines(preview: str, *, max_lines: int | None = None) -> list[str]:
    limit = max_lines if max_lines is not None else EXPLORER_TRACE_PREVIEW_LINES
    text = (preview or "").strip()
    if not text:
        return []
    return _indent_block(text, prefix="  │ ", max_lines=limit)


def _normalize_trace_row(event: str, data: dict[str, Any]) -> dict[str, Any]:
    """Ensure common schema fields on every trace row."""
    row: dict[str, Any] = {
        "ts": data.pop("ts", None) or datetime.now(timezone.utc).isoformat(),
        "event": event,
        "req_id": str(data.pop("req_id", None) or _req_id(str(data.get("req_id") or ""))),
        "flow_id": str(data.pop("flow_id", None) or _flow_id(str(data.get("flow_id") or ""))),
        "turn_index": int(data.pop("turn_index", data.get("turn_index", 0)) or 0),
        "phase": str(data.pop("phase", data.get("phase", "")) or ""),
        "query": str(data.pop("query", data.get("query", "")) or "")[:500],
        "decision": data.pop("decision", data.get("decision", "")),
        "tool_name": str(data.pop("tool_name", data.get("tool_name", data.get("tool", ""))) or ""),
        "tool_args": dict(data.pop("tool_args", data.get("tool_args", None)) or {}),
        "result_summary": str(data.pop("result_summary", data.get("result_summary", "")) or "")[:800],
        "reason": str(data.pop("reason", data.get("reason", "")) or "")[:500],
    }
    row.update(data)
    if not row["tool_name"] and row.get("tool"):
        row["tool_name"] = str(row.get("tool") or "")
    if not row["result_summary"] and row.get("result_preview"):
        row["result_summary"] = str(row.get("result_preview") or "")[:800]
    return row


def write_explorer_trace(event: str, **data: Any) -> None:
    """Append one NDJSON line (+ optional human flow) — single SSOT writer."""
    global _boot_logged
    if not EXPLORER_TRACE_ENABLED:
        return
    path = _resolve_path()
    row = _normalize_trace_row(event, dict(data))

    fmt = EXPLORER_TRACE_FORMAT
    write_ndjson = fmt in ("ndjson", "both")
    line = json.dumps(row, ensure_ascii=False, default=str) + "\n"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if write_ndjson:
            with _lock:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
        _emit_flow(row)
        if not _boot_logged:
            _boot_logged = True
            LOG.info(
                "explorer trace path=%s format=%s stdout=%s",
                path,
                EXPLORER_TRACE_FORMAT,
                EXPLORER_TRACE_STDOUT,
            )
    except OSError as exc:
        LOG.warning("explorer trace write failed path=%s err=%s", path, exc)


def trace_explorer(event: str, **data: Any) -> None:
    """Backward-compatible alias for write_explorer_trace."""
    write_explorer_trace(event, **data)


def diagnose_trace_file(path: Path | str | None = None) -> dict[str, Any]:
    """Inspect trace file health for CLI diagnostics."""
    p = Path(path) if path else _resolve_path()
    diag: dict[str, Any] = {
        "path": str(p),
        "exists": p.is_file(),
        "readable": False,
        "line_count": 0,
        "malformed_lines": 0,
        "events": {},
        "schema_issues": [],
        "flow_ids": [],
        "status": "missing",
    }
    if not p.is_file():
        diag["message"] = "trace file missing"
        return diag
    if not os.access(p, os.R_OK):
        diag["message"] = "trace file not readable"
        return diag
    diag["readable"] = True
    required = ("ts", "event")
    flow_ids: set[str] = set()
    try:
        with p.open(encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                diag["line_count"] += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    diag["malformed_lines"] += 1
                    continue
                if not isinstance(row, dict):
                    diag["malformed_lines"] += 1
                    continue
                ev = str(row.get("event") or "")
                diag["events"][ev] = diag["events"].get(ev, 0) + 1
                missing = [k for k in required if k not in row]
                if missing and i <= 5:
                    diag["schema_issues"].append(f"line {i} missing {missing}")
                fid = str(row.get("flow_id") or "")
                if fid:
                    flow_ids.add(fid)
    except OSError as exc:
        diag["message"] = f"read error: {exc}"
        diag["status"] = "error"
        return diag

    diag["flow_ids"] = sorted(flow_ids)[-20:]
    if diag["line_count"] == 0:
        diag["status"] = "empty"
        diag["message"] = "trace empty"
    elif diag["malformed_lines"] and diag["malformed_lines"] == diag["line_count"]:
        diag["status"] = "malformed"
        diag["message"] = "malformed ndjson"
    elif diag["schema_issues"]:
        diag["status"] = "schema_mismatch"
        diag["message"] = "schema mismatch"
    else:
        diag["status"] = "ok"
        diag["message"] = "ok"
    return diag


def format_flow_event(row: dict[str, Any]) -> str | None:
    """Render one trace row as a Cursor-like debug transcript block."""
    event = str(row.get("event") or "")
    if not event:
        return None

    ts = _short_ts(str(row.get("ts") or ""))
    flow_id = str(row.get("flow_id") or row.get("req_id") or "")
    fid = f" · {flow_id}" if flow_id else ""
    phase = str(row.get("phase") or "")
    phase_s = f" · {phase}" if phase else ""
    lines: list[str] = []

    if event in ("planner.runtime_state.created",):
        lines.append(f"[{ts}] runtime state{phase_s}{fid}")
        summary = str(row.get("result_summary") or row.get("runtime_state_summary") or "")
        if summary:
            lines.extend(_indent_block(summary, prefix="  ", max_lines=8))
        return "\n".join(lines)

        return "\n".join(lines)

    if event == "planner.triple_compared":
        lines.append(
            f"[{ts}] triple compare · "
            f"rule={row.get('rule_action')} heuristic={row.get('heuristic_action')} "
            f"llm={row.get('llm_action')}{phase_s}{fid}"
        )
        if row.get("action_match_rule_llm") is not None:
            lines.append(
                f"  action_match: rule↔llm={row.get('action_match_rule_llm')} "
                f"heuristic↔llm={row.get('action_match_heuristic_llm')}"
            )
        if row.get("target_overlap_rule_llm") is not None:
            lines.append(f"  target_overlap(rule↔llm): {row.get('target_overlap_rule_llm')}")
        if row.get("would_change_hot_path"):
            lines.append("  ⚠ would_change_hot_path=true")
        summary = str(row.get("result_summary") or "").strip()
        if summary:
            lines.append(f"  {summary[:240]}")
        return "\n".join(lines)

    if event in ("planner.shadow.proposed", "planner.shadow.compared", "planner.llm.proposed"):
        action = str(row.get("decision") or row.get("action") or "")
        lines.append(f"[{ts}] {event} · {action}{phase_s}{fid}")
        reason = str(row.get("reason") or "").strip()
        if reason:
            lines.append(f"  reason: {reason[:240]}")
        targets = row.get("target_files") or row.get("tool_args", {}).get("path")
        if targets:
            lines.append(f"  targets: {targets}")
        if event == "planner.shadow.compared":
            match = row.get("match")
            mismatch = row.get("mismatch_reason") or row.get("mismatch_reasons")
            if match is not None:
                lines.append(f"  match: {match}")
            if mismatch:
                lines.append(f"  mismatch: {mismatch}")
            if row.get("would_change_hot_path"):
                lines.append("  ⚠ would_change_hot_path=true")
        return "\n".join(lines)

    if event in (
        "planner.promotion.evaluated",
        "planner.promotion.blocked",
        "planner.promotion.eligible",
        "planner.promotion.applied",
        "planner.promotion.skipped",
    ):
        eligible = row.get("eligible")
        applied = row.get("applied")
        label = (
            "applied"
            if event.endswith("applied")
            else "skipped"
            if event.endswith("skipped")
            else "eligible"
            if event.endswith("eligible")
            else "blocked"
            if event.endswith("blocked")
            else "evaluated"
        )
        action = str(row.get("allowed_action") or row.get("effective_action") or "none")
        lines.append(f"[{ts}] promotion {label} · {action}{phase_s}{fid}")
        reason = str(row.get("reason") or "").strip()
        if reason:
            lines.append(f"  reason: {reason[:240]}")
        blocked = row.get("blocked_reasons")
        if blocked:
            lines.append(f"  blocked: {blocked}")
        if row.get("target"):
            lines.append(f"  target: {row.get('target')}")
        if row.get("original_rule_action"):
            orig = row.get("original_rule_action") or {}
            if isinstance(orig, dict) and orig.get("tool"):
                lines.append(f"  original_rule: {orig.get('tool')} {orig.get('target', '')}")
        if row.get("would_change_hot_path"):
            lines.append("  ⚠ would_change_hot_path=true")
        dry = row.get("dry_run_tool_call") or row.get("promotion_decision", {}).get("dry_run_tool_call") or {}
        fn = (dry.get("function") or {}) if isinstance(dry, dict) else {}
        if fn.get("name"):
            lines.append(f"  dry_run: {fn.get('name')}")
        metrics = row.get("metrics") or {}
        if isinstance(metrics, dict) and metrics.get("eligible_rate") is not None:
            lines.append(
                f"  metrics: eligible_rate={metrics.get('eligible_rate')} "
                f"blocked_action={metrics.get('blocked_by_action')}"
            )
        return "\n".join(lines)

    if event in ("tool.requested", "tool.completed"):
        cmd = _action_command(
            str(row.get("tool_name") or row.get("tool") or ""),
            path=str(row.get("path") or ""),
            tool_args=row.get("tool_args") if isinstance(row.get("tool_args"), dict) else {},
        )
        status = "requested" if event.endswith("requested") else "completed"
        lines.append(f"[{ts}] tool {status} · {cmd}{fid}")
        summary = str(row.get("result_summary") or "").strip()
        if summary:
            lines.extend(_preview_lines(summary, max_lines=6))
        return "\n".join(lines)

    if event in ("memory.evidence.upserted", "memory.journal.appended"):
        target = str(row.get("path") or row.get("target") or row.get("tool_name") or "")
        lines.append(f"[{ts}] {event} · {target}{fid}")
        summary = str(row.get("result_summary") or row.get("reason") or "")
        if summary:
            lines.append(f"  {summary[:200]}")
        return "\n".join(lines)

    if event in ("working_set.created", "coverage.checked", "final_report.rendered"):
        lines.append(f"[{ts}] {event}{phase_s}{fid}")
        summary = str(row.get("result_summary") or row.get("reason") or "")
        if summary:
            lines.append(f"  {summary[:300]}")
        return "\n".join(lines)

    if event == "plan":
        step = row.get("step", "?")
        source = str(row.get("decision_source") or row.get("decision") or "planner")
        header = f"[{ts}] plan · step {step} · {source}{fid}"
        lines.append(header)

        thinking = str(row.get("thinking") or "").strip()
        if thinking:
            lines.append("  thinking")
            lines.extend(_indent_block(thinking, prefix="    ", max_lines=16))

        next_tool = str(row.get("next_tool") or row.get("tool_name") or "")
        if next_tool == "answer" or row.get("depth_ok"):
            lines.append("  → final answer (depth sufficient)")
        elif next_tool:
            cmd = _action_command(
                next_tool,
                str(row.get("next_sid") or row.get("source_id") or ""),
                str(row.get("next_pattern") or row.get("pattern") or ""),
                str(row.get("next_glob") or row.get("glob_pattern") or ""),
            )
            lines.append(f"  → {cmd}")

        reason = str(row.get("reason") or "").strip()
        if reason and reason != thinking[: len(reason)]:
            lines.append(f"  reason: {reason[:240]}")

        pending = row.get("checklist_pending") or []
        if pending:
            shown = ", ".join(str(x) for x in pending[:5])
            extra = len(pending) - 5
            tail = f" (+{extra})" if extra > 0 else ""
            lines.append(f"  checklist: {shown}{tail}")

        return "\n".join(lines)

    if event == "action_emit":
        cmd = _action_command(
            str(row.get("tool") or row.get("tool_name") or ""),
            str(row.get("source_id") or ""),
            str(row.get("pattern") or ""),
            str(row.get("glob_pattern") or ""),
            tool_args=row.get("tool_args") if isinstance(row.get("tool_args"), dict) else {},
        )
        override = " override" if row.get("override") else ""
        lines.append(f"[{ts}] run{override}{fid}")
        lines.append(f"  → {cmd}")
        return "\n".join(lines)

    if event in ("action_done", "action_failed", "action_blocked"):
        tool = str(row.get("tool") or row.get("tool_name") or "")
        cmd = _action_command(
            tool,
            str(row.get("source_id") or ""),
            str(row.get("pattern") or ""),
            str(row.get("glob_pattern") or ""),
            tool_args=row.get("tool_args") if isinstance(row.get("tool_args"), dict) else {},
        )
        ok = row.get("success")
        if event == "action_blocked":
            status = "blocked"
        elif ok is False:
            status = "failed"
        elif ok is True:
            status = "done"
        else:
            status = event.replace("action_", "")

        chars = int(row.get("result_chars") or 0)
        meta = f" · {chars:,} chars" if chars else ""
        sig = str(row.get("action_sig") or "")
        if sig:
            meta += f" · {sig}"

        lines.append(f"[{ts}] {status} · {cmd}{meta}{fid}")

        guard = str(row.get("guard_reason") or "").strip()
        if guard:
            lines.append(f"  guard: {guard[:200]}")

        preview = str(row.get("result_preview") or row.get("result_summary") or "").strip()
        if preview:
            lines.append("  result")
            lines.extend(_preview_lines(preview))

        return "\n".join(lines)

    if event == "final_promote":
        lines.append(f"[{ts}] final · synthesize answer{fid}")
        thinking = str(row.get("thinking") or "").strip()
        if thinking:
            lines.append("  thinking")
            lines.extend(_indent_block(thinking, prefix="    ", max_lines=8))
        return "\n".join(lines)

    if event == "final_synthesis":
        lines.append(f"[{ts}] final · answer emitted{fid}")
        thinking = str(row.get("thinking") or "").strip()
        if thinking:
            lines.append("  explorer thinking (carried)")
            lines.extend(_indent_block(thinking, prefix="    ", max_lines=8))
        return "\n".join(lines)

    # Generic fallback for unknown events
    lines.append(f"[{ts}] {event}{phase_s}{fid}")
    for key in ("decision", "tool_name", "reason", "result_summary"):
        val = str(row.get(key) or "").strip()
        if val:
            lines.append(f"  {key}: {val[:240]}")
    return "\n".join(lines) if len(lines) > 1 else None


def _emit_flow(row: dict[str, Any]) -> None:
    global _last_plan_key
    if not EXPLORER_TRACE_STDOUT:
        return
    fmt = EXPLORER_TRACE_FORMAT
    if fmt not in ("flow", "both"):
        return

    event = str(row.get("event") or "")
    if event == "action_emit":
        return

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
        if key == _last_plan_key:
            return
        _last_plan_key = key

    block = format_flow_event(row)
    if not block:
        return
    try:
        with _lock:
            _flow_out.write(block + "\n\n")
            _flow_out.flush()
    except OSError as exc:
        LOG.warning("explorer flow stdout failed err=%s", exc)


def trace_explorer_plan(
    *,
    step: int,
    final_ready: bool,
    depth_ok: bool,
    thinking: str,
    decision_source: str,
    next_tool: str,
    next_sid: str,
    next_pattern: str = "",
    next_glob: str = "",
    reason: str = "",
    checklist: dict[str, str] | None = None,
    checklist_pending: list[str] | None = None,
    dims: dict[str, str] | None = None,
    tried_tail: list[str] | None = None,
    tried_count: int = 0,
    phase: str = "",
    query: str = "",
    turn_index: int = 0,
) -> None:
    trace_explorer(
        "plan",
        step=step,
        final_ready=final_ready,
        depth_ok=depth_ok,
        thinking=(thinking or "")[:4000],
        decision_source=decision_source,
        decision=decision_source,
        next_tool=next_tool or ("answer" if depth_ok else ""),
        tool_name=next_tool or ("answer" if depth_ok else ""),
        next_sid=next_sid,
        next_pattern=(next_pattern or "")[:120],
        next_glob=(next_glob or "")[:80],
        reason=(reason or "")[:400],
        checklist=checklist or {},
        checklist_pending=checklist_pending or [],
        dims=dims or {},
        tried_count=tried_count,
        tried_tail=(tried_tail or [])[-12:],
        phase=phase,
        query=query,
        turn_index=turn_index,
    )


def trace_explorer_action(
    phase: str,
    *,
    tool: str,
    source_id: str = "",
    pattern: str = "",
    glob_pattern: str = "",
    override: bool | None = None,
    guard_reason: str = "",
    success: bool | None = None,
    result_chars: int = 0,
    result_preview: str = "",
    action_sig: str = "",
    thinking: str = "",
    query: str = "",
    turn_index: int = 0,
    tool_args: dict[str, Any] | None = None,
) -> None:
    preview = (result_preview or "")[:EXPLORER_TRACE_PREVIEW_CHARS]
    trace_explorer(
        phase,
        tool=tool,
        tool_name=tool,
        source_id=source_id,
        pattern=(pattern or "")[:120],
        glob_pattern=(glob_pattern or "")[:80],
        tool_args=dict(tool_args or {}),
        override=override,
        guard_reason=(guard_reason or "")[:240],
        success=success,
        result_chars=result_chars,
        result_preview=preview,
        result_summary=preview[:800],
        action_sig=(action_sig or "")[:160],
        thinking=(thinking or "")[:400],
        phase=phase if phase not in ("action_done", "action_failed", "action_blocked", "action_emit") else "",
        query=query,
        turn_index=turn_index,
    )


def format_ndjson_file(path: Path | str, *, from_offset: int = 0, flow_id: str = "") -> str:
    """Format an entire NDJSON trace file (or tail from byte offset)."""
    p = Path(path)
    if not p.is_file():
        return ""
    blocks: list[str] = []
    last_plan_key = ""
    with p.open(encoding="utf-8") as fh:
        if from_offset:
            fh.seek(from_offset)
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
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
                blocks.append(block)
    return "\n\n".join(blocks)


def replay_trace_file(
    path: Path | str,
    *,
    from_start: bool = True,
    flow_id: str = "",
) -> list[str]:
    """Read trace file and return formatted blocks (defensive parsing)."""
    p = Path(path)
    if not p.is_file():
        return []
    blocks: list[str] = []
    last_plan_key = ""
    with p.open(encoding="utf-8") as fh:
        if not from_start:
            fh.seek(0, 2)
            return blocks
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
                blocks.append(block)
    return blocks
