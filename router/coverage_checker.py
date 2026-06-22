"""Coverage check — must_include, symbol targets, truncation, evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from context_need import ContextNeed

SYMBOL_TARGET_RE = re.compile(r"^([^:]+)::(.+)$")
COVERAGE_THRESHOLD = float(__import__("os").getenv("COVERAGE_THRESHOLD", "0.75"))


@dataclass
class CoverageReport:
    coverage_score: float = 1.0
    complete: bool = True
    missing: list[str] = field(default_factory=list)
    truncated: list[dict[str, Any]] = field(default_factory=list)
    action: str = "proceed"
    critical_source_truncated: bool = False
    latest_tool_result_missing: bool = False
    symbol_missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage_score": self.coverage_score,
            "complete": self.complete,
            "missing": list(self.missing),
            "truncated": list(self.truncated),
            "action": self.action,
            "critical_source_truncated": self.critical_source_truncated,
            "latest_tool_result_missing": self.latest_tool_result_missing,
            "symbol_missing": list(self.symbol_missing),
        }


MUST_INCLUDE_ALIASES: dict[str, tuple[str, ...]] = {
    "current user request": ("[task]", "user_query", "active_query", "current user"),
    "current_user_task": ("[task]", "user_query", "active_query"),
    "active agent plan": ("[saved agent plan]", "agent plan", "next_action", "task_intent"),
    "active_agent_plan": ("[saved agent plan]", "agent plan", "next_action"),
    "latest tool result": (
        "tool result",
        '"role": "tool"',
        "[retrieved",
        "exit code:",
        "grep",
        "workspace_result",
    ),
    "latest_tool_result": (
        "tool result",
        '"role": "tool"',
        "[retrieved",
    ),
    "session context": ("session_state", "session_tail", "[delta"),
    "document content": ("[retrieved", "file_read", "document"),
}


def _prompt_text(packed_prompt: Any) -> str:
    if packed_prompt is None:
        return ""
    if isinstance(packed_prompt, str):
        return packed_prompt
    if hasattr(packed_prompt, "full_text"):
        return str(packed_prompt.full_text or "")
    body = getattr(packed_prompt, "body", None) or packed_prompt
    if isinstance(body, dict):
        parts: list[str] = []
        for msg in body.get("messages", []):
            if isinstance(msg, dict):
                parts.append(str(msg.get("content", "")))
        must = getattr(packed_prompt, "must_include_block", "") or ""
        if must:
            parts.insert(0, must)
        return "\n".join(parts)
    return str(body)


def _retrieval_blob(retrieval_pack: Any) -> str:
    items = getattr(retrieval_pack, "items", None) or []
    return "\n".join(str(getattr(i, "content", "") or "") for i in items).lower()


def check_must_include(need: ContextNeed, prompt_text: str) -> list[str]:
    missing: list[str] = []
    text = prompt_text.lower()
    for item in need.must_include or []:
        key = item.lower().replace("_", " ")
        aliases = MUST_INCLUDE_ALIASES.get(key, MUST_INCLUDE_ALIASES.get(item.lower(), (key,)))
        if not any(a.lower() in text for a in aliases):
            missing.append(item)
    return missing


def check_symbol_targets(
    need: ContextNeed,
    prompt_text: str,
    retrieval_pack: Any,
) -> list[str]:
    """file.py::function_name style targets."""
    missing: list[str] = []
    text = prompt_text.lower()
    blob = _retrieval_blob(retrieval_pack)
    combined = text + "\n" + blob

    for target in need.coverage_targets or []:
        m = SYMBOL_TARGET_RE.match(str(target).strip())
        if not m:
            continue
        path_part, symbol = m.group(1).lower(), m.group(2).lower()
        path_ok = path_part in text or path_part in blob
        sym_ok = (
            f"def {symbol}" in combined
            or f"class {symbol}" in combined
            or f"function {symbol}" in combined
            or symbol in combined
        )
        if not (path_ok and sym_ok):
            missing.append(target)
    return missing


def _target_covered(
    target: str,
    *,
    text: str,
    retrieved_sources: set[str],
    items: list[Any],
    coverage_target_in_text: Any,
    source_hits: list[str] | None = None,
    coverage_hits: list[str] | None = None,
) -> bool:
    t = str(target).lower()
    if coverage_target_in_text and coverage_target_in_text(target, text):
        return True
    if t in text:
        return True

    hit_blob = [str(h) for h in (source_hits or []) + (coverage_hits or [])]
    if hit_blob:
        if any(str(h).lower() == t for h in hit_blob):
            return True
        if coverage_target_in_text and any(coverage_target_in_text(target, h) for h in hit_blob):
            return True

    if coverage_target_in_text:
        if any(coverage_target_in_text(target, src) for src in retrieved_sources):
            return True
        if any(
            coverage_target_in_text(target, str(getattr(i, "content", "")))
            for i in items
        ):
            return True
    elif any(t in src for src in retrieved_sources):
        return True
    elif any(t in str(getattr(i, "content", "")).lower() for i in items):
        return True
    return False


def check_file_targets(
    need: ContextNeed,
    retrieval_pack: Any,
    prompt_text: str,
    *,
    source_hits: list[str] | None = None,
    coverage_hits: list[str] | None = None,
) -> list[str]:
    missing: list[str] = []
    text = prompt_text.lower()
    items = getattr(retrieval_pack, "items", None) or []
    retrieved_sources = {str(getattr(i, "source", "")).lower() for i in items}

    try:
        from reference.target_coverage import coverage_target_in_text
    except ImportError:
        coverage_target_in_text = None  # type: ignore[assignment]

    for target in need.coverage_targets or []:
        if SYMBOL_TARGET_RE.match(str(target).strip()):
            continue
        if _target_covered(
            target,
            text=text,
            retrieved_sources=retrieved_sources,
            items=items,
            coverage_target_in_text=coverage_target_in_text,
            source_hits=source_hits,
            coverage_hits=coverage_hits,
        ):
            continue
        missing.append(target)

    pack_missing = list(getattr(retrieval_pack, "missing_targets", None) or [])
    for m in pack_missing:
        if m not in missing and not SYMBOL_TARGET_RE.match(str(m).strip()):
            missing.append(m)
    return missing


def check_truncation_loss(truncation_markers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    critical: list[dict[str, Any]] = []
    for mark in truncation_markers or []:
        lost = int(mark.get("lost_tokens", 0) or 0)
        src = str(mark.get("source", "")).lower()
        if lost <= 32 and src in ("plan", "system", "state", "delta", "session", "session_tail"):
            continue
        if mark.get("critical") or mark.get("must_include"):
            critical.append(mark)
        elif lost > 500:
            critical.append(mark)
    return critical


@dataclass
class CoverageFailDetail:
    item: str
    reason: str
    tier: str = ""
    budget_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "reason": self.reason,
            "tier": self.tier,
            "budget_truncated": self.budget_truncated,
        }


def _item_in_text(item: str, text: str) -> bool:
    key = item.lower().replace("_", " ")
    if key in text:
        return True
    aliases = MUST_INCLUDE_ALIASES.get(key, MUST_INCLUDE_ALIASES.get(item.lower(), (key,)))
    return any(a.lower() in text for a in aliases)


def _classify_tier(*, source: str = "", section: str = "") -> str:
    src = (source or section or "").lower()
    if src in ("session", "session_tail", "delta", "state", "plan"):
        return "session"
    if src in ("retrieved", "vector", "retrieval") or "vector" in src:
        return "vector"
    if src in ("policy", "failed_action", "failed_tool"):
        return "policy"
    if src in ("artifact", "tool_result", "file_read", "shell_result", "tool_context"):
        return "artifact"
    if src in ("system", "prompt", "prompt_pack", "current_task"):
        return "prompt_pack"
    if "file_read" in src or "tool_result" in src:
        return "artifact"
    if "retrieved" in src:
        return "vector"
    return "prompt_pack"


def analyze_coverage_fail_reasons(
    context_need: ContextNeed,
    retrieval_pack: Any,
    packed_prompt: Any,
    coverage: CoverageReport | None = None,
    *,
    expected_targets: list[str] | None = None,
) -> list[CoverageFailDetail]:
    """Break down coverage misses by root cause for quality gate / investor audit."""
    report = coverage or check_coverage(context_need, retrieval_pack, packed_prompt)
    text = _prompt_text(packed_prompt).lower()
    blob = _retrieval_blob(retrieval_pack)
    markers = list(getattr(packed_prompt, "truncation_markers", None) or report.truncated or [])
    missing_targets = {
        str(m).lower() for m in (getattr(retrieval_pack, "missing_targets", None) or [])
    }
    need_targets = {str(t).lower() for t in (context_need.coverage_targets or [])}
    need_must = {str(m).lower() for m in (context_need.must_include or [])}
    expected = {str(t).lower() for t in (expected_targets or [])}

    details: list[CoverageFailDetail] = []
    for item in report.missing:
        item_l = item.lower()
        reason = "retrieval_miss"
        tier = "vector"
        budget_truncated = False
        path_part = item_l.split("::")[0] if "::" in item_l else item_l

        if item_l.startswith("evidence:"):
            reason = "retrieval_miss"
            tier = "artifact"
        elif item_l in ("latest tool result", "latest_tool_result") or report.latest_tool_result_missing:
            reason = "latest_tool_missing"
            tier = "artifact"
            if _item_in_text("grep", blob) or _item_in_text("tool", blob):
                reason = "prompt_exclusion"
                tier = "prompt_pack"
        elif item in (report.symbol_missing or []) or SYMBOL_TARGET_RE.match(str(item).strip()):
            reason = "symbol_missing"
            m = SYMBOL_TARGET_RE.match(str(item).strip())
            path_part = m.group(1).lower() if m else item_l.split("::")[0]
            sym = m.group(2).lower() if m else item_l.split("::")[-1]
            tier = "vector"
            if path_part in blob and sym in blob and sym not in text:
                reason = "prompt_exclusion"
                tier = "prompt_pack"
            elif path_part in blob and sym not in blob:
                reason = "budget_truncation"
                budget_truncated = True
                tier = "vector"
            elif item_l in missing_targets or path_part not in blob:
                reason = "retrieval_miss"
                tier = "vector"
        elif item_l in need_must and item_l not in need_targets:
            if _item_in_text(item, blob) and not _item_in_text(item, text):
                reason = "prompt_exclusion"
                tier = "prompt_pack"
            elif not _item_in_text(item, blob) and not _item_in_text(item, text):
                if item_l in expected and item_l not in need_must:
                    reason = "need_missing"
                    tier = "session"
                else:
                    reason = "prompt_exclusion"
                    tier = "prompt_pack"
        elif item_l in missing_targets or (
            item_l in need_targets and item_l not in text and item_l not in blob
        ):
            reason = "retrieval_miss"
            tier = "vector"
        elif item_l in blob and item_l not in text:
            reason = "prompt_exclusion"
            tier = "prompt_pack"

        for mark in markers:
            src = str(mark.get("source", "")).lower()
            if not src:
                continue
            if item_l in src or src in item_l or path_part in src:
                reason = "budget_truncation"
                budget_truncated = True
                tier = _classify_tier(source=src)
                break

        if report.critical_source_truncated and reason == "retrieval_miss" and item_l in blob:
            reason = "budget_truncation"
            budget_truncated = True

        details.append(
            CoverageFailDetail(
                item=item,
                reason=reason,
                tier=tier,
                budget_truncated=budget_truncated,
            )
        )
    return details


def check_coverage(
    context_need: ContextNeed,
    retrieval_pack: Any,
    packed_prompt: Any,
    *,
    truncation_markers: list[dict[str, Any]] | None = None,
    evidence_needed: list[str] | None = None,
    evidence_collected: list[str] | None = None,
    source_hits: list[str] | None = None,
    coverage_hits: list[str] | None = None,
) -> CoverageReport:
    """Check whether required evidence appears in the packed prompt."""
    text = _prompt_text(packed_prompt).lower()
    markers = truncation_markers
    if markers is None and hasattr(packed_prompt, "truncation_markers"):
        markers = list(packed_prompt.truncation_markers or [])

    missing_must = check_must_include(context_need, text)
    missing_files = check_file_targets(
        context_need,
        retrieval_pack,
        text,
        source_hits=source_hits,
        coverage_hits=coverage_hits,
    )
    missing_symbols = check_symbol_targets(context_need, text, retrieval_pack)
    truncated = check_truncation_loss(markers or [])

    missing = list(dict.fromkeys(missing_must + missing_files + missing_symbols))

    if evidence_needed:
        collected = set(str(c).split(":")[0] for c in (evidence_collected or []))
        coll_all = list(evidence_collected or [])
        for ev in evidence_needed:
            key = str(ev).split(":")[0]
            if key.lower() in text:
                continue
            if key in collected:
                continue
            if key == "target_coverage" and any(str(c).startswith("source_hit:") for c in coll_all):
                continue
            if key == "target_coverage" and "target_coverage" in coll_all:
                continue
            if key == "target_coverage" and (source_hits or coverage_hits):
                targets = [str(t) for t in (context_need.coverage_targets or []) if t]
                hits = set(str(h).lower() for h in (source_hits or []) + (coverage_hits or []))
                try:
                    from reference.target_coverage import coverage_target_in_text
                except ImportError:
                    coverage_target_in_text = None  # type: ignore[assignment,misc]
                if targets and all(
                    str(t).lower() in hits
                    or (coverage_target_in_text and any(coverage_target_in_text(t, h) for h in hits))
                    for t in targets
                ):
                    continue
            missing.append(f"evidence:{ev}")

    n_checks = max(
        1,
        len(context_need.must_include or [])
        + len([t for t in (context_need.coverage_targets or []) if not SYMBOL_TARGET_RE.match(str(t))])
        + len(missing_symbols)
        + (len(evidence_needed or []) if evidence_needed else 0),
    )
    penalty = len(missing) + len(truncated) * 0.5
    score = max(0.0, 1.0 - penalty / n_checks)

    critical_trunc = bool(truncated)
    latest_tool_missing = "latest tool result" in missing_must or "latest_tool_result" in missing_must

    complete = not missing and not truncated and score >= COVERAGE_THRESHOLD
    action = "proceed"
    if missing_must:
        action = "ask_tool" if latest_tool_missing else "re_retrieve"
    elif missing_symbols or missing_files:
        action = "re_retrieve"
    elif truncated:
        action = "increase_budget"
    if not complete and action == "proceed":
        action = "increase_budget"

    return CoverageReport(
        coverage_score=round(score, 3),
        complete=complete,
        missing=missing,
        truncated=truncated,
        action=action,
        critical_source_truncated=critical_trunc,
        latest_tool_result_missing=latest_tool_missing,
        symbol_missing=missing_symbols,
    )
