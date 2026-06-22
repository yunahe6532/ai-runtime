"""Dedicated read-only explorer trace — thinking + actions, separate from router main log."""

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
EXPLORER_TRACE_PATH = os.getenv(
    "EXPLORER_TRACE_PATH",
    "/captures/explorer-trace.ndjson",
)
# ndjson | flow | both — flow = human-readable Cursor-like transcript
EXPLORER_TRACE_FORMAT = os.getenv("EXPLORER_TRACE_FORMAT", "both").strip().lower()
EXPLORER_TRACE_STDOUT = os.getenv("EXPLORER_TRACE_STDOUT", "1") == "1"
EXPLORER_TRACE_PREVIEW_LINES = int(os.getenv("EXPLORER_TRACE_PREVIEW_LINES", "14"))
EXPLORER_TRACE_PREVIEW_CHARS = int(os.getenv("EXPLORER_TRACE_PREVIEW_CHARS", "2400"))

_lock = threading.Lock()
_boot_logged = False
_last_plan_key = ""
_flow_out: TextIO = sys.stderr


def _resolve_path() -> Path:
    return Path(EXPLORER_TRACE_PATH)


def _flow_id() -> str:
    try:
        from adapters.observe import current_run_id

        return str(current_run_id() or "")
    except ImportError:
        return ""


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
) -> str:
    label = _tool_label(tool)
    target = source_id or path
    parts = [label]
    if target:
        parts.append(target)
    if glob_pattern:
        parts.append(glob_pattern)
    elif pattern:
        parts.append(f'"{pattern[:100]}"')
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


def format_flow_event(row: dict[str, Any]) -> str | None:
    """Render one trace row as a Cursor-like debug transcript block."""
    event = str(row.get("event") or "")
    if not event:
        return None

    ts = _short_ts(str(row.get("ts") or ""))
    flow_id = str(row.get("flow_id") or "")
    fid = f" · {flow_id}" if flow_id else ""
    lines: list[str] = []

    if event == "plan":
        step = row.get("step", "?")
        source = str(row.get("decision_source") or "planner")
        header = f"[{ts}] plan · step {step} · {source}{fid}"
        lines.append(header)

        thinking = str(row.get("thinking") or "").strip()
        if thinking:
            lines.append("  thinking")
            lines.extend(_indent_block(thinking, prefix="    ", max_lines=16))

        next_tool = str(row.get("next_tool") or "")
        if next_tool == "answer" or row.get("depth_ok"):
            lines.append("  → final answer (depth sufficient)")
        elif next_tool:
            cmd = _action_command(
                next_tool,
                str(row.get("next_sid") or ""),
                str(row.get("next_pattern") or ""),
                str(row.get("next_glob") or ""),
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
            str(row.get("tool") or ""),
            str(row.get("source_id") or ""),
            str(row.get("pattern") or ""),
            str(row.get("glob_pattern") or ""),
        )
        override = " override" if row.get("override") else ""
        lines.append(f"[{ts}] run{override}{fid}")
        lines.append(f"  → {cmd}")
        return "\n".join(lines)

    if event in ("action_done", "action_failed", "action_blocked"):
        tool = str(row.get("tool") or "")
        cmd = _action_command(
            tool,
            str(row.get("source_id") or ""),
            str(row.get("pattern") or ""),
            str(row.get("glob_pattern") or ""),
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

        preview = str(row.get("result_preview") or "").strip()
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

    return None


def _emit_flow(row: dict[str, Any]) -> None:
    global _last_plan_key
    if not EXPLORER_TRACE_STDOUT:
        return
    fmt = EXPLORER_TRACE_FORMAT
    if fmt not in ("flow", "both"):
        return

    event = str(row.get("event") or "")
    # plan already shows the intended next tool — skip redundant emit line
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


def trace_explorer(event: str, **data: Any) -> None:
    """Append one NDJSON line (+ optional human flow) to explorer trace."""
    global _boot_logged
    if not EXPLORER_TRACE_ENABLED:
        return
    path = _resolve_path()
    row: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "flow_id": data.pop("flow_id", None) or _flow_id(),
    }
    row.update(data)

    fmt = EXPLORER_TRACE_FORMAT
    write_ndjson = fmt in ("ndjson", "both")
    line = json.dumps(row, ensure_ascii=False, default=str) + "\n"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            if write_ndjson:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
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
) -> None:
    trace_explorer(
        "plan",
        step=step,
        final_ready=final_ready,
        depth_ok=depth_ok,
        thinking=(thinking or "")[:4000],
        decision_source=decision_source,
        next_tool=next_tool or ("answer" if depth_ok else ""),
        next_sid=next_sid,
        next_pattern=(next_pattern or "")[:120],
        next_glob=(next_glob or "")[:80],
        reason=(reason or "")[:400],
        checklist=checklist or {},
        checklist_pending=checklist_pending or [],
        dims=dims or {},
        tried_count=tried_count,
        tried_tail=(tried_tail or [])[-12:],
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
) -> None:
    preview = (result_preview or "")[:EXPLORER_TRACE_PREVIEW_CHARS]
    trace_explorer(
        phase,
        tool=tool,
        source_id=source_id,
        pattern=(pattern or "")[:120],
        glob_pattern=(glob_pattern or "")[:80],
        override=override,
        guard_reason=(guard_reason or "")[:240],
        success=success,
        result_chars=result_chars,
        result_preview=preview,
        action_sig=(action_sig or "")[:160],
        thinking=(thinking or "")[:400],
    )


def format_ndjson_file(path: Path | str, *, from_offset: int = 0) -> str:
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
