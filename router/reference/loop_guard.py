"""Progress / bad ping-pong detection for agent tool loops."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

LOG = logging.getLogger("router.loop_guard")

BAD_PING_PONG_TURNS = int(__import__("os").getenv("BAD_PING_PONG_TURNS", "6"))
SAME_ACTION_REPEAT_LIMIT = int(__import__("os").getenv("SAME_ACTION_REPEAT_LIMIT", "2"))
XML_LEAK_LIMIT = int(__import__("os").getenv("XML_LEAK_LIMIT", "2"))
FINAL_WITHOUT_EVIDENCE_LIMIT = int(__import__("os").getenv("FINAL_WITHOUT_EVIDENCE_LIMIT", "2"))


@dataclass
class ProgressSnapshot:
    evidence_count: int = 0
    artifact_count: int = 0
    files_read_count: int = 0
    plan_step: int = 0
    source_hits: int = 0


def _query_hash(query: str) -> str:
    return hashlib.sha256((query or "").strip().encode()).hexdigest()[:16]


def reset_loop_counters(state: Any, query: str = "") -> None:
    """Reset per-user-turn loop counters (new user message)."""
    state.final_answer_count = 0
    state.xml_leak_count = 0
    state.final_without_evidence_count = 0
    state.turns_since_progress = 0
    state.same_action_repeated = 0
    state.last_action_sig = ""
    state.explore_round = 0
    state.judge_round = 0
    state.tools_since_judge = 0
    state.evidence_items = []
    state.missing_evidence = []
    if query:
        state.active_query_hash = _query_hash(query)


def snapshot_progress(state: Any) -> ProgressSnapshot:
    ap = getattr(state, "agent_plan", None) or {}
    collected = ap.get("evidence_collected") or []
    return ProgressSnapshot(
        evidence_count=len(collected),
        artifact_count=len(getattr(state, "artifacts", None) or []),
        files_read_count=len(getattr(state, "files_read", None) or []),
        plan_step=int(ap.get("step_count") or 0),
        source_hits=len(ap.get("source_hits") or []),
    )


def is_good_progress(before: ProgressSnapshot, after: ProgressSnapshot) -> bool:
    return (
        after.evidence_count > before.evidence_count
        or after.artifact_count > before.artifact_count
        or after.files_read_count > before.files_read_count
        or after.plan_step > before.plan_step
        or after.source_hits > before.source_hits
    )


def record_turn_progress(state: Any, before: ProgressSnapshot, after: ProgressSnapshot) -> None:
    if is_good_progress(before, after):
        state.turns_since_progress = 0
        LOG.info(
            "loop_progress evidence=%d→%d files=%d→%d step=%d→%d",
            before.evidence_count,
            after.evidence_count,
            before.files_read_count,
            after.files_read_count,
            before.plan_step,
            after.plan_step,
        )
    else:
        state.turns_since_progress = int(getattr(state, "turns_since_progress", 0) or 0) + 1


def record_action_sig(state: Any, sig: str) -> None:
    sig = (sig or "").strip()
    if not sig:
        return
    if sig == getattr(state, "last_action_sig", ""):
        state.same_action_repeated = int(getattr(state, "same_action_repeated", 0) or 0) + 1
    else:
        state.same_action_repeated = 0
        state.last_action_sig = sig


def record_xml_leak(state: Any) -> None:
    state.xml_leak_count = int(getattr(state, "xml_leak_count", 0) or 0) + 1


def record_final_without_evidence(state: Any) -> None:
    state.final_without_evidence_count = int(getattr(state, "final_without_evidence_count", 0) or 0) + 1


def record_final_answer_sent(state: Any) -> None:
    state.final_answer_count = int(getattr(state, "final_answer_count", 0) or 0) + 1


def is_bad_ping_pong(state: Any) -> bool:
    if int(getattr(state, "turns_since_progress", 0) or 0) >= BAD_PING_PONG_TURNS:
        return True
    if int(getattr(state, "same_action_repeated", 0) or 0) >= SAME_ACTION_REPEAT_LIMIT:
        return True
    if int(getattr(state, "xml_leak_count", 0) or 0) >= XML_LEAK_LIMIT:
        return True
    if int(getattr(state, "final_without_evidence_count", 0) or 0) >= FINAL_WITHOUT_EVIDENCE_LIMIT:
        return True
    return False


def should_block_final_answer(
    state: Any | None,
    *,
    can_final: bool,
    task_intent: str = "",
    intent_name: str = "",
) -> tuple[bool, str]:
    """Return (blocked, reason)."""
    if state is None:
        return False, ""
    if is_bad_ping_pong(state):
        return True, "bad_ping_pong"
    if int(getattr(state, "final_answer_count", 0) or 0) >= 1:
        return True, "final_already_sent_this_turn"
    if not can_final:
        record_final_without_evidence(state)
        return True, "evidence_incomplete"

    runtime = dict(getattr(state, "last_runtime_turn", None) or {})
    metrics = dict(getattr(state, "last_ingest_metrics", None) or {})

    if runtime.get("coverage_complete") is False:
        score = float(runtime.get("coverage_score", metrics.get("coverage_score", 0)) or 0)
        recovered = bool(runtime.get("recovery_recovered"))
        if score < float(__import__("os").getenv("COVERAGE_THRESHOLD", "0.75")) and not recovered:
            return True, "coverage_incomplete"

    if runtime.get("latest_tool_result_missing"):
        return True, "latest_tool_result_missing"

    if runtime.get("critical_source_truncated"):
        triggered = bool(runtime.get("recovery_triggered"))
        recovered = bool(runtime.get("recovery_recovered"))
        score = float(runtime.get("coverage_score", metrics.get("coverage_score", 0)) or 0)
        threshold = float(__import__("os").getenv("COVERAGE_THRESHOLD", "0.75"))
        if score >= threshold:
            pass
        elif not triggered or not recovered:
            return True, "critical_source_truncated"

    judge = dict(getattr(state, "last_judge_decision", None) or {})
    if judge and judge.get("allow_final") is False:
        return True, "evidence_judge_insufficient"

    if task_intent == "general" and intent_name not in ("casual", "explain"):
        ap = getattr(state, "agent_plan", None) or {}
        if not ap.get("evidence_needed"):
            return True, "general_no_evidence_plan"

    if metrics.get("coverage_complete") is False:
        score = float(metrics.get("coverage_score", 0) or 0)
        threshold = float(__import__("os").getenv("COVERAGE_THRESHOLD", "0.75"))
        recovered = bool(metrics.get("recovery_recovered"))
        if score < threshold and not recovered:
            return True, "coverage_insufficient"

    return False, ""


def emit_plan_repair(reason: str) -> None:
    try:
        from adapters.observe import current_run_id, emit_task

        rid = current_run_id()
        if rid:
            emit_task(rid, "plan.repair", reason[:240])
    except ImportError:
        pass
