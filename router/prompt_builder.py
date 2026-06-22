"""State + delta prompt builder (steps 4, 6, 8)."""

from __future__ import annotations

import copy
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from reference.agent_exec import (
    EXEC_INTENTS,
    apply_stream_policy,
    system_for_intent,
)
from capture import _content_text, _sha256
from context_budget import (
    BudgetPlan,
    ContextBudget,
    allocate_static,
    truncate_to_token_budget,
)
from context_cache import ContextIndex, extract_last_user_query
from adapters.memory import Artifact, RequestDelta, SessionState, normalize_file_path
from message_index import classify_message_kind
from reference.plan_state import build_plan_state, format_plan_state_block
from reference.planner import ensure_agent_plan, format_saved_agent_plan_block
from legacy.retriever import (
    RetrievalPack,
    format_retrieval_pack,
    format_retrieved,
    pack_to_chunks,
    retrieve_artifacts,
)
from vl_pass import message_has_image

MEMORY_STATE_BODY = os.getenv("MEMORY_STATE_BODY", "1") == "1"
from runtime_kernel.constants import TOOL_PLANNING_MAX_TOKENS
EXEC_CONTEXT_INTENTS = frozenset({"code_edit", "benchmark", "shell_task", "log_analysis", "agent", "bugfix"})
RECENT_AGENT_MSG_KEEP = int(os.getenv("RECENT_AGENT_MSG_KEEP", "8"))
RECENT_AGENT_MSG_CHARS = int(os.getenv("RECENT_AGENT_MSG_CHARS", "8000"))
TOOL_TAIL_MAX_CHARS = int(os.getenv("TOOL_TAIL_MAX_CHARS", "1200"))

LOG = logging.getLogger("router.prompt_builder")

DYNAMIC_BUDGET = os.getenv("DYNAMIC_BUDGET", "1") == "1"


@dataclass
class PromptPack:
    body: dict[str, Any]
    phase: str
    budget: BudgetPlan | None = None
    must_include_block: str = ""
    truncation_markers: list[dict[str, Any]] = field(default_factory=list)
    tokens_used: dict[str, int] = field(default_factory=dict)
    coverage: Any = None
    prompt_sources: dict[str, str] = field(default_factory=dict)

    @property
    def full_text(self) -> str:
        parts = [self.must_include_block]
        for msg in self.body.get("messages", []):
            if isinstance(msg, dict):
                parts.append(str(msg.get("content", "")))
        return "\n".join(p for p in parts if p)


def _truncate_with_marker(
    text: str,
    budget_tokens: int,
    *,
    source: str,
    critical: bool = False,
    markers: list[dict[str, Any]],
) -> str:
    if budget_tokens <= 0:
        markers.append({
            "source": source,
            "reason": "budget_zero",
            "lost_tokens": estimate_text_tokens(text),
            "critical": critical,
        })
        return ""
    max_chars = budget_tokens * 3
    if len(text) <= max_chars:
        return text
    lost = estimate_text_tokens(text) - budget_tokens
    markers.append({
        "source": source,
        "reason": "budget_exceeded",
        "lost_tokens": max(0, lost),
        "critical": critical,
        "must_include": critical,
    })
    return text[:max_chars] + f"\n[TRUNCATED: {source}]\nreason: budget exceeded\nlost_tokens: {lost}"


def estimate_text_tokens(text: str) -> int:
    return max(1, len(text or "") // 3)


def _extract_user_query_text(body: dict[str, Any], query: str = "") -> str:
    messages = body.get("messages", [])
    if isinstance(messages, list):
        for msg in reversed(messages):
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            text = _content_text(msg.get("content", ""))
            m = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.S)
            if m:
                return m.group(1).strip()
    return query or extract_last_user_query(body)


def _compact_system(body: dict[str, Any], intent_name: str, phase: str, budget_tokens: int) -> str:
    base = system_for_intent(intent_name, phase=phase)
    original = extract_original_system(body)
    if original:
        orig_text = _content_text(original.get("content", ""))
        rules_m = re.search(r"<user_rules>.*?</user_rules>", orig_text, re.S)
        if rules_m:
            base += "\n\n" + rules_m.group(0)
    return truncate_to_token_budget(base, budget_tokens)


def extract_original_system(body: dict[str, Any]) -> dict[str, Any] | None:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return None
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            return copy.deepcopy(msg)
    return None


def extract_recent_agent_tail(
    body: dict[str, Any],
    max_messages: int | None = None,
    max_chars: int | None = None,
) -> list[dict[str, Any]]:
    max_messages = RECENT_AGENT_MSG_KEEP if max_messages is None else max_messages
    max_chars = RECENT_AGENT_MSG_CHARS if max_chars is None else max_chars
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return []
    tail: list[dict[str, Any]] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("assistant", "tool"):
            continue
        out = copy.deepcopy(msg)
        content = _content_text(msg.get("content", ""))
        if len(content) > max_chars:
            content = content[:max_chars] + "\n...(truncated)"
        out["content"] = content
        tail.insert(0, out)
        if len(tail) >= max_messages:
            break
    return tail


def _state_summary_block(state: SessionState, budget_tokens: int) -> str:
    ws = state.effective_workspace or state.workspace_path or state.project_key
    lines = [
        "[session_state]",
        f"project: {ws}",
        f"chat: {state.chat_id or '-'}",
        f"reason: {state.session_reason or 'continue'}",
        f"requests: {state.total_requests}",
    ]
    if state.current_query:
        lines.append(f"active_query: {state.current_query[:300]}")
    if state.phase_hint:
        lines.append(f"phase_hint: {state.phase_hint}")
    if state.files_read:
        lines.append("files_read: " + ", ".join(state.files_read[-12:]))
    if state.commands_run:
        lines.append("commands_run: " + ", ".join(state.commands_run[-8:]))
    if state.artifacts:
        lines.append(f"artifact_index: {len(state.artifacts)} stored")
    lines.append("[/session_state]")
    return truncate_to_token_budget("\n".join(lines), budget_tokens)


def _delta_block(delta: RequestDelta, budget_tokens: int) -> str:
    if not delta.added_count:
        return ""
    lines = [f"[delta +{delta.added_count}]"]
    for dm in delta.added[-6:]:
        extra = f" tools={dm.tool_calls}" if dm.tool_calls else ""
        lines.append(f"- [{dm.role}]{extra} {dm.preview[:160]}")
    lines.append("[/delta]")
    return truncate_to_token_budget("\n".join(lines), budget_tokens)


def _resolve_current_query(
    body: dict[str, Any],
    state: SessionState | None,
    index: ContextIndex | None,
    query: str = "",
) -> tuple[str, str]:
    """current_query: state → context_index → body fallback."""
    if state and state.current_query:
        return state.current_query, "canonical"
    if index and index.query:
        return index.query, "canonical"
    extracted = _extract_user_query_text(body, query)
    if extracted:
        return extracted, "body_fallback"
    return query or "", "body_fallback"


def _delta_tool_result_messages(
    body: dict[str, Any],
    delta: RequestDelta | None,
) -> list[tuple[Any, dict[str, Any]]]:
    """Collect tool_result messages from delta.added (not last_role alone)."""
    if not delta or not delta.added:
        return []
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return []
    out: list[tuple[Any, dict[str, Any]]] = []
    for dm in delta.added:
        if dm.role != "tool":
            continue
        if not (0 <= dm.index < len(messages)):
            continue
        msg = messages[dm.index]
        if not isinstance(msg, dict):
            continue
        if classify_message_kind(msg) != "tool_result":
            continue
        out.append((dm, msg))
    return out


def _has_pending_tool_result(body: dict[str, Any], delta: RequestDelta | None) -> bool:
    if _delta_tool_result_messages(body, delta):
        return True
    messages = body.get("messages", [])
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict) and classify_message_kind(last) == "tool_result":
            return True
    return False


def _build_tool_context(
    body: dict[str, Any],
    budget_tokens: int,
    state: SessionState | None = None,
    *,
    delta: RequestDelta | None = None,
    artifacts: list[Artifact] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """tool_context: delta tool_result → artifact summary → body fallback."""
    if not _has_pending_tool_result(body, delta):
        return [], "none"

    per_msg_cap = min(TOOL_TAIL_MAX_CHARS, max(400, budget_tokens * 2))
    msgs: list[dict[str, Any]] = []
    source = "none"

    delta_tools = _delta_tool_result_messages(body, delta)
    if delta_tools:
        dm, msg = delta_tools[-1]
        text = dm.preview or _content_text(msg.get("content", ""))
        name = dm.tool_name or str(msg.get("name") or "tool")
        msgs = [{"role": "tool", "name": name, "content": text}]
        source = "delta"

    if artifacts:
        art = artifacts[-1]
        if art.analysis.get("kind") != "failed_action":
            from artifact_excerpt import artifact_prompt_text

            text = artifact_prompt_text(art, per_msg_cap)
            if not text.strip():
                text = art.path or ""
            msg = {"role": "tool", "name": art.name or art.type, "content": text}
            msgs = [_compact_tail_message(msg, per_msg_cap, state=state)]
            source = "artifact"

    if not msgs:
        tail = extract_recent_agent_tail(body, max_messages=2, max_chars=per_msg_cap)
        tool_tail = [m for m in tail if m.get("role") == "tool"]
        if tool_tail:
            msgs = [_compact_tail_message(m, per_msg_cap, state=state) for m in tool_tail[-1:]]
            source = "body_fallback"
        elif tail:
            msgs = [_compact_tail_message(m, per_msg_cap, state=state) for m in tail]
            source = "body_fallback"

    return msgs, source


def _read_only_tool_planning_tail(
    body: dict[str, Any],
    budget_tokens: int,
    state: SessionState | None = None,
    *,
    delta: RequestDelta | None = None,
    artifacts: list[Artifact] | None = None,
) -> list[dict[str, Any]]:
    """Recent tool results for read-only tool_planning — avoids blind repeat reads."""
    per_msg_cap = min(TOOL_TAIL_MAX_CHARS, max(400, budget_tokens // 3))
    msgs: list[dict[str, Any]] = []
    used = 0
    max_chars = budget_tokens * 3

    if artifacts:
        for art in reversed(artifacts[-6:]):
            if art.type not in ("file_read", "tool_result", "shell_result"):
                continue
            if art.analysis.get("kind") == "failed_action":
                continue
            from artifact_excerpt import artifact_prompt_text

            text = artifact_prompt_text(art, per_msg_cap)
            if not text.strip():
                continue
            name = art.name or art.type
            if art.path:
                text = f"path={art.path}\n{text}"
            compact = _compact_tool_result(text, min(per_msg_cap, 1200), state=state, path_hint=art.path or "")
            if used + len(compact) > max_chars:
                break
            msgs.insert(0, {"role": "tool", "name": name, "content": compact})
            used += len(compact)
            if len(msgs) >= 4:
                break

    if msgs:
        return msgs

    if _has_pending_tool_result(body, delta):
        delta_tools = _delta_tool_result_messages(body, delta)
        for dm, msg in delta_tools[-4:]:
            text = dm.preview or _content_text(msg.get("content", ""))
            name = dm.tool_name or str(msg.get("name") or "tool")
            compact = _compact_tool_result(text, per_msg_cap, state=state)
            msgs.append({"role": "tool", "name": name, "content": compact})
    return msgs


def _load_session_evidence_artifacts(
    state: SessionState | None,
    artifacts: list[Artifact] | None = None,
) -> list[Artifact]:
    """All successful evidence artifacts for this chat — not just the current delta."""
    from legacy.retriever import load_artifact_meta

    out: list[Artifact] = []
    seen: set[str] = set()
    current_chat = getattr(state, "chat_id", "") or "" if state else ""
    project_key = getattr(state, "project_key", "") or "" if state else ""

    for aid in reversed(getattr(state, "artifacts", None) or []):
        if aid in seen:
            continue
        art = load_artifact_meta(aid, project_key)
        if not art:
            continue
        if art.type not in ("file_read", "tool_result", "shell_result"):
            continue
        if art.is_error and art.analysis.get("kind") == "failed_action":
            continue
        art_chat = getattr(art, "chat_id", "") or ""
        if current_chat and art_chat and art_chat != current_chat:
            continue
        out.append(art)
        seen.add(aid)

    for art in artifacts or []:
        if art.artifact_id in seen:
            continue
        if art.type not in ("file_read", "tool_result", "shell_result"):
            continue
        if art.is_error and art.analysis.get("kind") == "failed_action":
            continue
        out.append(art)
        seen.add(art.artifact_id)
    return out


def _final_answer_evidence_budget(budget_plan: BudgetPlan) -> int:
    """Pool plan/state/delta slots into one evidence budget for final synthesis."""
    return (
        budget_plan.retrieved
        + budget_plan.artifact
        + budget_plan.session_tail
        + budget_plan.state
        + budget_plan.plan
        + budget_plan.delta
    )


def _pack_final_answer_evidence(
    artifacts: list[Artifact],
    budget_tokens: int,
    *,
    phase: str = "final_answer",
    coverage_targets: list[str] | None = None,
    state: SessionState | None = None,
) -> str:
    """Merge coverage artifacts — tier digest merge when budget allows."""
    evidence = _load_session_evidence_artifacts(state, artifacts)
    if budget_tokens <= 0 or not evidence:
        return ""

    from artifact_excerpt import pack_tier_evidence_for_final

    tier_block, _digests = pack_tier_evidence_for_final(
        evidence,
        budget_tokens,
        phase=phase,
        coverage_targets=coverage_targets,
    )
    if tier_block.strip():
        return tier_block

    from artifact_excerpt import artifact_prompt_text

    try:
        from reference.target_coverage import coverage_target_in_text
    except ImportError:
        coverage_target_in_text = None  # type: ignore[assignment]

    targets = [t.lower() for t in (coverage_targets or []) if t]

    ordered: list[Artifact] = []
    seen_ids: set[str] = set()
    if targets:
        for t in targets:
            best: Artifact | None = None
            for art in evidence:
                src = art.path or art.name or ""
                hit = (
                    coverage_target_in_text(t, src)
                    if coverage_target_in_text
                    else t in src.lower()
                )
                if hit and (best is None or art.chars > best.chars):
                    best = art
            if best and best.artifact_id not in seen_ids:
                ordered.append(best)
                seen_ids.add(best.artifact_id)
    for art in reversed(evidence):
        if art.artifact_id not in seen_ids:
            ordered.append(art)
            seen_ids.add(art.artifact_id)

    n = max(1, len(ordered))
    per_item = max(1024, budget_tokens // n)
    parts: list[str] = ["[collected_evidence]"]
    remaining = budget_tokens
    for art in ordered:
        if remaining <= 128:
            break
        item_budget = min(per_item, remaining)
        text = artifact_prompt_text(art, item_budget, phase=phase)
        if not text.strip():
            continue
        header = art.path or art.name or art.artifact_id
        parts.append(f"### {header}")
        parts.append(text)
        remaining -= estimate_text_tokens(text)
    parts.append("[/collected_evidence]")
    block = "\n\n".join(parts).strip()
    if block and block != "[collected_evidence]\n\n[/collected_evidence]":
        LOG.info(
            "final_evidence_pack items=%d budget=%d used_est=%d phase=%s",
            len(ordered),
            budget_tokens,
            budget_tokens - max(0, remaining),
            phase,
        )
    return block


def _canonical_session_tail(
    artifacts: list[Artifact],
    budget_tokens: int,
    state: SessionState | None = None,
    *,
    phase: str = "final_answer",
    coverage_targets: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build final_answer tail from stored artifact raw text — not ingest summaries."""
    if not artifacts:
        return []
    from legacy.retriever import _prompt_content

    evidence = [
        a
        for a in artifacts
        if a.type in ("file_read", "tool_result", "shell_result")
        and not (a.is_error and a.analysis.get("kind") == "failed_action")
    ]
    if not evidence:
        return []

    block = _pack_final_answer_evidence(
        artifacts,
        budget_tokens,
        phase=phase,
        coverage_targets=coverage_targets,
        state=state,
    )
    if block:
        return [{"role": "tool", "name": "collected_evidence", "content": block}]

    max_chars = budget_tokens * 3
    per_cap = min(12000, max(1500, max_chars // max(1, min(4, len(evidence)))))
    out: list[dict[str, Any]] = []
    used = 0
    for art in reversed(evidence[-8:]):
        content = _prompt_content(art, per_cap // 3, phase=phase)
        if not content.strip():
            continue
        if used + len(content) > max_chars:
            break
        out.insert(
            0,
            {
                "role": "tool",
                "name": art.name or art.type,
                "content": content,
            },
        )
        used += len(content)
        if len(out) >= 6:
            break
    return out


def _session_tail_body_fallback(
    body: dict[str, Any],
    budget_tokens: int,
    state: SessionState | None = None,
) -> list[dict[str, Any]]:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return []

    last_user_idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user_idx = i
    if last_user_idx < 0:
        return []

    tail = messages[last_user_idx + 1 :]
    if not tail:
        return []

    max_chars = budget_tokens * 3
    out: list[dict[str, Any]] = []
    used = 0
    seen_fp: set[str] = set()
    for msg in reversed(tail):
        if not isinstance(msg, dict):
            continue
        m = copy.deepcopy(msg)
        content = _content_text(m.get("content", ""))
        fp = _sha256(content[:400])[:16] if content else m.get("role", "")
        if fp in seen_fp:
            continue
        seen_fp.add(fp)
        per_cap = min(4000, max(800, max_chars // 4))
        if m.get("role") == "tool" and len(content) > per_cap:
            content = _compact_tool_result(content, per_cap, state=state)
        elif len(content) > per_cap:
            content = content[:per_cap] + "\n...(truncated)"
        if used + len(content) > max_chars:
            break
        m["content"] = content
        out.insert(0, m)
        used += len(content)
        if len(out) >= 6:
            break
    return out


def _build_session_tail(
    body: dict[str, Any],
    phase: str,
    budget_tokens: int,
    state: SessionState | None = None,
    *,
    artifacts: list[Artifact] | None = None,
    coverage_targets: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    if phase != "final_answer" and phase != "partial_final_answer":
        return [], "none"

    canonical = _canonical_session_tail(
        artifacts or [],
        budget_tokens,
        state=state,
        phase=phase,
        coverage_targets=coverage_targets,
    )
    if canonical:
        return canonical, "artifact"

    fallback = _session_tail_body_fallback(body, budget_tokens, state=state)
    if fallback:
        return fallback, "body_fallback"
    return [], "canonical"


# Backward-compatible alias for tests
_artifact_session_tail = _canonical_session_tail


def _session_tail_messages(
    body: dict[str, Any],
    phase: str,
    budget_tokens: int,
    state: SessionState | None = None,
    *,
    artifacts: list[Artifact] | None = None,
) -> list[dict[str, Any]]:
    tail, _ = _build_session_tail(
        body, phase, budget_tokens, state=state, artifacts=artifacts
    )
    return tail


def _compact_tool_result(
    content: str,
    max_chars: int,
    state: SessionState | None = None,
    path_hint: str = "",
    *,
    prefer_raw: bool = False,
) -> str:
    """Shrink large tool outputs — never substitute index preview for prompt text."""
    _ = state, path_hint, prefer_raw

    if len(content) <= max_chars:
        return content
    lines = content.splitlines()
    kind = "file"
    if content.lstrip().startswith("<!DOCTYPE") or "<html" in content[:300].lower():
        kind = "html"
        # Keep head + tail for HTML (structure at both ends)
        head_n = min(16, len(lines))
        tail_n = min(8, max(0, len(lines) - head_n))
        parts = ["\n".join(lines[:head_n])]
        if tail_n:
            parts.append(f"...({len(lines) - head_n - tail_n} lines omitted)...")
            parts.append("\n".join(lines[-tail_n:]))
        head = "\n".join(parts)
    elif "<workspace_result" in content[:400]:
        kind = "grep"
        line_budget = max(40, max_chars // 80)
        head = "\n".join(lines[:line_budget])
    elif "Exit code:" in content[:200]:
        kind = "shell"
        head = "\n".join(lines[:24])
    else:
        head = "\n".join(lines[:16])
    return (
        f"[tool result {kind} truncated chars={len(content)} lines={len(lines)}]\n"
        f"{head}\n...(full content in artifact cache; do NOT re-Read — use plan state summary)"
    )


def _compact_tail_message(msg: dict[str, Any], max_chars: int, state: SessionState | None = None) -> dict[str, Any]:
    out = copy.deepcopy(msg)
    role = out.get("role")
    if role == "assistant":
        out["content"] = ""
        return out
    if role == "tool":
        text = _content_text(out.get("content", ""))
        path_hint = str(out.get("name") or "")
        out["content"] = _compact_tool_result(text, max_chars, state=state, path_hint=path_hint)
    return out


def _tool_planning_tail(
    body: dict[str, Any],
    budget_tokens: int,
    state: SessionState | None = None,
    *,
    delta: RequestDelta | None = None,
    artifacts: list[Artifact] | None = None,
) -> list[dict[str, Any]]:
    """tool_context only — separate from session_tail."""
    msgs, _ = _build_tool_context(
        body, budget_tokens, state=state, delta=delta, artifacts=artifacts
    )
    return msgs


def _last_user_message(
    body: dict[str, Any],
    query: str,
    state: SessionState | None = None,
) -> dict[str, Any]:
    if state and state.current_query:
        return {"role": "user", "content": state.current_query}
    if query:
        return {"role": "user", "content": query}
    messages = body.get("messages", [])
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                text = _content_text(msg.get("content", ""))
                if "<user_query>" in text:
                    return copy.deepcopy(msg)
    return {"role": "user", "content": query or extract_last_user_query(body)}


def build_with_budget(
    body: dict[str, Any],
    state: SessionState,
    delta: RequestDelta,
    artifacts: list[Artifact],
    intent_name: str,
    phase: str,
    backend: str,
    index: ContextIndex,
    query: str = "",
    *,
    budget_plan: BudgetPlan | None = None,
    retrieval_pack: RetrievalPack | None = None,
    context_need: Any = None,
) -> PromptPack:
    """Build proxy body from BudgetPlan + optional RetrievalPack."""
    markers: list[dict[str, Any]] = []
    query, query_source = _resolve_current_query(body, state, index, query or index.query)
    phase = phase or "tool_planning"
    max_out = TOOL_PLANNING_MAX_TOKENS if phase == "tool_planning" else int(
        body.get("max_tokens") or 4096
    )
    if budget_plan is None:
        budget_plan = allocate_static(backend, phase, max_out)
    budget = ContextBudget.from_plan(budget_plan)

    if retrieval_pack and retrieval_pack.items:
        retrieved_text = format_retrieval_pack(retrieval_pack)
    else:
        retrieved_chunks = retrieve_artifacts(state, query, delta, budget_plan.retrieved)
        retrieved_text = format_retrieved(retrieved_chunks)

    agent_plan = ensure_agent_plan(state, query)
    plan = build_plan_state(state, query, artifacts)
    plan_text = format_saved_agent_plan_block(agent_plan, budget_plan.plan, state=state)
    legacy_plan = format_plan_state_block(plan, max(128, budget_plan.plan // 2))
    state_text = _state_summary_block(state, budget_plan.state)
    delta_text = _delta_block(delta, budget_plan.delta)

    must_items = list(getattr(context_need, "must_include", None) or [])
    coverage_targets = list(getattr(context_need, "coverage_targets", None) or [])
    must_include_block = ""
    if must_items:
        must_include_block = "[Must Include]\n" + "\n".join(f"- {m}" for m in must_items)

    tool_context_msgs, tool_source = _build_tool_context(
        body, budget_plan.artifact, state=state, delta=delta, artifacts=artifacts
    )
    session_tail_msgs, session_source = _build_session_tail(
        body,
        phase,
        budget_plan.session_tail,
        state=state,
        artifacts=artifacts,
        coverage_targets=coverage_targets,
    )

    final_evidence_text = ""
    if phase in ("final_answer", "partial_final_answer", "recovery_final"):
        evidence_budget = _final_answer_evidence_budget(budget_plan)
        final_evidence_text = _pack_final_answer_evidence(
            artifacts,
            evidence_budget,
            phase=phase,
            coverage_targets=coverage_targets,
            state=state,
        )

    prompt_sources = {
        "query": query_source,
        "tool_context": tool_source,
        "session_tail": session_source,
        "budget_mode": budget_plan.mode,
    }
    state.last_prompt_sources = prompt_sources
    LOG.info(
        "prompt_source query=%s tool_context=%s session_tail=%s phase=%s intent=%s budget=%s",
        query_source,
        tool_source,
        session_source,
        phase,
        intent_name,
        budget_plan.mode,
    )

    out = copy.deepcopy(body)
    proxy_messages: list[dict[str, Any]] = []
    user_msg = _last_user_message(body, query, state=state)
    tokens_used: dict[str, int] = {}

    if phase in ("final_answer", "partial_final_answer", "recovery_final"):
        prose_phase = "final_answer"
        evidence_budget = _final_answer_evidence_budget(budget_plan)
        sys_parts = [system_for_intent(intent_name, phase=prose_phase)]
        if must_include_block:
            sys_parts.insert(0, must_include_block)
        if final_evidence_text:
            sys_parts.append(
                _truncate_with_marker(
                    final_evidence_text,
                    evidence_budget,
                    source="collected_evidence",
                    critical=True,
                    markers=markers,
                )
            )
        elif retrieved_text:
            sys_parts.append(
                _truncate_with_marker(
                    retrieved_text,
                    evidence_budget,
                    source="retrieved",
                    critical=True,
                    markers=markers,
                )
            )
        sys_cap = budget_plan.system + evidence_budget
        sys_content = truncate_to_token_budget("\n\n".join(x for x in sys_parts if x), sys_cap)
        tokens_used["system"] = estimate_text_tokens(sys_content)
        tokens_used["collected_evidence"] = max(0, tokens_used["system"] - estimate_text_tokens(sys_parts[0]))
        proxy_messages.append({"role": "system", "content": sys_content})

        task_lines = [
            f"[Task]\n{query}",
            "",
            "Answer using ALL collected_evidence and quote_bank line citations in the system message.",
            "Structure (from evidence only — do not invent paths or line numbers):",
            "1) Official docs tier definitions if doc evidence exists",
            "2) Per tier (runtime_core, adapters, legacy, integrations): role table with file→responsibility",
            "3) 2–3 code citations per tier as ```path Lstart-Lend: snippet``` from quote_bank",
            "4) Import/boundary sentences from grep evidence",
            "5) ASCII or mermaid relationship diagram (import direction only if evidenced)",
            "6) One-line summary table per directory",
            "If quote_bank or path L123: is missing for a claim, say evidence gap — do not guess.",
        ]
        if intent_name == "read_only_analysis":
            task_body = "\n".join(task_lines)
        else:
            task_body = (
                f"[Task]\n{query}\n\nAnswer using ALL collected_evidence in the system message. "
                "Cover every listed directory with file paths and one-line roles from evidence. "
                "Do not invent paths not present in evidence."
            )
        task_content = _truncate_with_marker(
            task_body,
            budget_plan.current_task,
            source="current_task",
            critical=True,
            markers=markers,
        )
        if message_has_image(user_msg):
            proxy_messages.append(user_msg)
        else:
            proxy_messages.append({"role": "user", "content": task_content})
        tokens_used["current_task"] = estimate_text_tokens(task_content)
        if final_evidence_text:
            session_tail_msgs = []
            session_source = "system_evidence"
        proxy_messages.extend(session_tail_msgs)
        tokens_used["session_tail"] = sum(estimate_text_tokens(_content_text(m.get("content", ""))) for m in session_tail_msgs)
        out["messages"] = proxy_messages
        apply_stream_policy(out, intent_name, False, False)
        out.pop("tools", None)
        out.pop("tool_choice", None)
        return PromptPack(
            body=out,
            phase=phase,
            budget=budget_plan,
            must_include_block=must_include_block,
            truncation_markers=markers,
            tokens_used=tokens_used,
            prompt_sources=prompt_sources,
        )

    if intent_name in ("explain", "casual"):
        sys_content = system_for_intent(intent_name, phase=None)
    else:
        sys_content = _compact_system(body, intent_name, phase or "tool_planning", budget_plan.system)
    extras = []
    if must_include_block:
        extras.append(must_include_block)
    if plan_text:
        extras.append(_truncate_with_marker(plan_text, budget_plan.plan, source="plan", critical=True, markers=markers))
    if legacy_plan:
        extras.append(_truncate_with_marker(legacy_plan, budget_plan.state // 2, source="legacy_plan", markers=markers))
    if state_text:
        extras.append(_truncate_with_marker(state_text, budget_plan.state, source="state", markers=markers))
    if delta_text:
        extras.append(_truncate_with_marker(delta_text, budget_plan.delta, source="delta", markers=markers))
    if retrieved_text:
        extras.append(_truncate_with_marker(retrieved_text, budget_plan.retrieved, source="retrieved", markers=markers))

    sys_cap = budget_plan.system + budget_plan.plan + budget_plan.state + budget_plan.delta + budget_plan.retrieved
    sys_content = truncate_to_token_budget(
        sys_content + "\n\n" + "\n\n".join(x for x in extras if x),
        sys_cap,
    )
    tokens_used["system"] = estimate_text_tokens(sys_content)
    proxy_messages.append({"role": "system", "content": sys_content})

    if intent_name in EXEC_CONTEXT_INTENTS or intent_name in ("agent", "debug"):
        proxy_messages.extend(tool_context_msgs)
        tokens_used["artifact"] = sum(
            estimate_text_tokens(_content_text(m.get("content", ""))) for m in tool_context_msgs
        )
    elif phase == "tool_planning" and intent_name == "read_only_analysis":
        tail_msgs = _read_only_tool_planning_tail(
            body, budget_plan.artifact, state=state, delta=delta, artifacts=artifacts,
        )
        proxy_messages.extend(tail_msgs)
        tokens_used["artifact"] = sum(
            estimate_text_tokens(_content_text(m.get("content", ""))) for m in tail_msgs
        )

    if message_has_image(user_msg):
        proxy_messages.append(user_msg)
    else:
        task_parts = [f"[Task]\n{query}"]
        if delta_text and intent_name not in EXEC_CONTEXT_INTENTS:
            task_parts.append(delta_text)
        task_content = _truncate_with_marker(
            "\n\n".join(task_parts),
            budget_plan.current_task,
            source="current_task",
            critical=True,
            markers=markers,
        )
        proxy_messages.append({"role": "user", "content": task_content})
        tokens_used["current_task"] = estimate_text_tokens(task_content)

    out["messages"] = proxy_messages
    needs_tools = intent_name in EXEC_INTENTS or intent_name in ("agent", "debug", "shell_task")
    apply_stream_policy(out, intent_name, needs_tools, intent_name in EXEC_INTENTS)

    if not needs_tools:
        out.pop("tools", None)
        out.pop("tool_choice", None)
        return PromptPack(
            body=out,
            phase=phase,
            budget=budget_plan,
            must_include_block=must_include_block,
            truncation_markers=markers,
            tokens_used=tokens_used,
            prompt_sources=prompt_sources,
        )

    if phase == "tool_planning":
        existing = out.get("max_tokens")
        if not isinstance(existing, int) or existing > TOOL_PLANNING_MAX_TOKENS:
            out["max_tokens"] = TOOL_PLANNING_MAX_TOKENS

    return PromptPack(
        body=out,
        phase=phase,
        budget=budget_plan,
        must_include_block=must_include_block,
        truncation_markers=markers,
        tokens_used=tokens_used,
        prompt_sources=prompt_sources,
    )


def build_memory_proxy_body(
    body: dict[str, Any],
    state: SessionState,
    delta: RequestDelta,
    artifacts: list[Artifact],
    intent_name: str,
    phase: str,
    backend: str,
    index: ContextIndex,
    query: str = "",
) -> tuple[dict[str, Any], str]:
    """Build proxy body — dynamic scheduler when enabled, else static BudgetPlan."""
    if DYNAMIC_BUDGET:
        from dynamic_context_scheduler import build_context_for_turn

        pack = build_context_for_turn(
            body, state, delta, artifacts, intent_name, phase, backend, index, query=query
        )
        return pack.body, pack.phase

    pack = build_with_budget(
        body, state, delta, artifacts, intent_name, phase, backend, index, query=query
    )
    return pack.body, pack.phase


def should_use_memory_body(intent_name: str, state: SessionState) -> bool:
    if not MEMORY_STATE_BODY:
        return False
    if state.agent_plan and (
        state.agent_plan.get("known_files") or state.agent_plan.get("task_intent")
    ):
        return True
    if intent_name in ("code_edit", "benchmark", "log_analysis", "agent", "debug", "shell_task"):
        return True
    return bool(state.project_key or state.workspace_path or state.session_id)


def inject_memory_context(
    proxy_body: dict[str, Any],
    state: SessionState,
    delta: RequestDelta,
    artifacts: list[Artifact],
) -> dict[str, Any]:
    """Legacy hook — memory body builder already embeds state; no-op if present."""
    messages = proxy_body.get("messages", [])
    if not isinstance(messages, list):
        return proxy_body
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            content = _content_text(msg.get("content", ""))
            if "[session_state]" in content:
                return proxy_body
    return proxy_body
