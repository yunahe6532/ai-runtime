"""Final Report Renderer — deterministic markdown from journal + anchors + handoff."""

from __future__ import annotations

from typing import Any


def _section(title: str, lines: list[str]) -> str:
    body = [ln for ln in lines if ln and ln.strip()]
    if not body:
        return ""
    return f"## {title}\n\n" + "\n".join(body) + "\n"


def _format_anchor_row(a: dict[str, Any]) -> str:
    path = str(a.get("path") or "")
    sym = str(a.get("symbol") or "")
    ls = int(a.get("line_start") or 0)
    le = int(a.get("line_end") or 0)
    summary = str(a.get("summary") or "").strip()
    loc = f"{path}"
    if sym:
        loc += f"::{sym}"
    if ls and le:
        loc += f" L{ls}-L{le}"
    elif ls:
        loc += f" L{ls}"
    row = f"- **{loc}**"
    if summary:
        row += f": {summary[:240]}"
    return row


def _journal_rows(journal: list[dict[str, Any]], *, limit: int = 15) -> list[str]:
    rows: list[str] = []
    for j in journal[-limit:]:
        if not isinstance(j, dict):
            continue
        kind = str(j.get("kind") or "?")
        target = str(j.get("target") or "")[:100]
        summary = str(j.get("summary") or "")[:160]
        why = str(j.get("why") or "")[:80]
        meta = j.get("meta") or {}
        extra = ""
        if meta.get("line_start"):
            extra = f" L{meta.get('line_start')}-L{meta.get('line_end', meta.get('line_start'))}"
        line = f"- `{kind}` **{target}**{extra}"
        if why:
            line += f" — {why}"
        if summary:
            line += f" — {summary}"
        rows.append(line)
    return rows


def _test_result_rows(state: Any, handoff: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for cmd in (handoff.get("commands_run") or getattr(state, "commands_run", None) or [])[-8:]:
        rows.append(f"- `{cmd}`")
    ap = getattr(state, "agent_plan", None) or {}
    for ev in (ap.get("evidence_collected") or handoff.get("evidence_collected") or [])[-10:]:
        ev_s = str(ev)
        if any(k in ev_s.lower() for k in ("test", "benchmark", "exit", "pass", "fail", "score")):
            rows.append(f"- evidence: {ev_s}")
    for j in (getattr(state, "task_journal", None) or [])[-20:]:
        if not isinstance(j, dict) or j.get("kind") != "shell":
            continue
        summary = str(j.get("summary") or "")
        if summary:
            rows.append(f"- shell: {summary[:200]}")
    return rows


def render_final_report(
    state: Any,
    *,
    query: str = "",
    handoff: dict[str, Any] | None = None,
    include_anchors: bool = True,
    max_anchors: int = 20,
) -> str:
    """Journal + handoff + evidence anchors → deterministic markdown."""
    ho = handoff or getattr(state, "handoff", None) or {}
    q = query or ho.get("query") or getattr(state, "current_query", "") or ""
    parts: list[str] = [f"# Task Report\n", f"**Query:** {q[:500]}\n"]

    touched = list(ho.get("files_read") or getattr(state, "files_read", None) or [])
    if touched:
        parts.append(_section("Touched Files", [f"- `{p}`" for p in touched[:20]]))

    if include_anchors:
        anchors = list(getattr(state, "evidence_anchors", None) or [])
        anchor_lines = [_format_anchor_row(a) for a in anchors[-max_anchors:] if isinstance(a, dict)]
        if anchor_lines:
            parts.append(_section("Evidence Anchors", anchor_lines))

    journal = list(ho.get("journal_tail") or getattr(state, "task_journal", None) or [])
    jrows = _journal_rows(journal)
    if jrows:
        parts.append(_section("Task Journal", jrows))

    test_rows = _test_result_rows(state, ho)
    if test_rows:
        parts.append(_section("Commands & Test Results", test_rows))

    risks = list(ho.get("remaining_risks") or [])
    ap = getattr(state, "agent_plan", None) or {}
    missing = list(ap.get("missing_evidence") or getattr(state, "missing_evidence", None) or [])
    risk_lines = [f"- {r}" for r in (risks or missing)[:8]]
    failed = ho.get("failed_actions") or getattr(state, "failed_actions", None) or {}
    for action, cnt in list(failed.items())[:5]:
        risk_lines.append(f"- failed `{action}` ×{cnt}")
    if risk_lines:
        parts.append(_section("Remaining Risks", risk_lines))

    collected = list(ap.get("evidence_collected") or ho.get("evidence_collected") or [])
    if collected:
        parts.append(_section("Evidence Collected", [f"- {c}" for c in collected[:12]]))

    coverage = list(ho.get("coverage_targets") or ap.get("preferred_sources") or [])[:10]
    if coverage:
        parts.append(_section("Coverage Targets", [f"- `{t}`" for t in coverage]))

    text = "\n".join(p for p in parts if p).strip()
    return text


def render_final_report_for_prompt(state: Any, *, query: str = "", max_chars: int = 12000) -> str:
    """Compact report block for system prompt / final_answer prefill."""
    report = render_final_report(state, query=query)
    if len(report) <= max_chars:
        return report
    return report[: max_chars - 80] + "\n\n...(report truncated for budget)\n"


def polish_optional(report: str) -> str:
    """Placeholder — LLM polish hook for Phase 3+. Returns report as-is."""
    return report
