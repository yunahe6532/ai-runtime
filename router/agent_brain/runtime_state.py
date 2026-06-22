"""Agent Brain — Planner input RuntimeState contract (Phase 2.0 SSOT)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from runtime_kernel.self_model import load_self_model

MAX_RUNTIME_STATE_PROMPT_CHARS = int(os.getenv("MAX_RUNTIME_STATE_PROMPT_CHARS", "8000"))
MAX_RUNTIME_STATE_JOURNAL_ITEMS = int(os.getenv("MAX_RUNTIME_STATE_JOURNAL_ITEMS", "12"))
MAX_RUNTIME_STATE_ANCHOR_ITEMS = int(os.getenv("MAX_RUNTIME_STATE_ANCHOR_ITEMS", "10"))
MAX_RUNTIME_STATE_WS_ITEMS = int(os.getenv("MAX_RUNTIME_STATE_WS_ITEMS", "12"))


@dataclass
class RuntimeStateLimits:
    max_runtime_state_prompt_chars: int = MAX_RUNTIME_STATE_PROMPT_CHARS
    max_journal_items: int = MAX_RUNTIME_STATE_JOURNAL_ITEMS
    max_anchor_items: int = MAX_RUNTIME_STATE_ANCHOR_ITEMS
    max_working_set_items: int = MAX_RUNTIME_STATE_WS_ITEMS


@dataclass
class RuntimeState:
    """Single planner input stack — compact view for LLM, raw for observability."""

    current_user_request: str = ""
    router_intent: str = ""
    context_intent: str = ""
    phase: str = "tool_planning"
    project_index_summary: dict[str, Any] = field(default_factory=dict)
    working_set_summary: dict[str, Any] = field(default_factory=dict)
    task_journal_tail: list[dict[str, Any]] = field(default_factory=list)
    evidence_anchor_summary: list[dict[str, Any]] = field(default_factory=list)
    handoff_summary: dict[str, Any] = field(default_factory=dict)
    runtime_self_model: dict[str, Any] = field(default_factory=dict)
    budget_state: dict[str, Any] = field(default_factory=dict)
    coverage_state: dict[str, Any] = field(default_factory=dict)
    last_tool_results: list[dict[str, Any]] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    available_actions: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if k != "raw"}

    def compact_dict(self) -> dict[str, Any]:
        return self.to_dict()

    def to_prompt_json(self, *, max_chars: int | None = None) -> str:
        cap = max_chars or MAX_RUNTIME_STATE_PROMPT_CHARS
        text = json.dumps(self.compact_dict(), ensure_ascii=False, default=str)
        if len(text) <= cap:
            return text
        trimmed = self._trim_for_budget(cap)
        text = json.dumps(trimmed, ensure_ascii=False, default=str)
        if len(text) <= cap:
            return text
        return text[: max(cap - 20, 0)] + ',"_truncated":true}'

    def _trim_for_budget(self, cap: int) -> dict[str, Any]:
        d = self.compact_dict()
        for key, n in (
            ("task_journal_tail", MAX_RUNTIME_STATE_JOURNAL_ITEMS // 2),
            ("evidence_anchor_summary", MAX_RUNTIME_STATE_ANCHOR_ITEMS // 2),
            ("last_tool_results", 4),
        ):
            if isinstance(d.get(key), list) and len(d[key]) > n:
                d[key] = d[key][-n:]
        d["handoff_summary"] = {
            k: d.get("handoff_summary", {}).get(k)
            for k in ("query", "phase_hint", "anchor_count", "journal_count")
            if d.get("handoff_summary")
        }
        text = json.dumps(d, ensure_ascii=False, default=str)
        if len(text) > cap:
            d["project_index_summary"] = {
                "file_count": d.get("project_index_summary", {}).get("file_count", 0),
                "entrypoints": (d.get("project_index_summary", {}).get("entrypoints") or [])[:3],
            }
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RuntimeState:
        if not data:
            return cls()
        fields = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**fields)


def _summarize_project_index(project_index: Any | None) -> dict[str, Any]:
    if project_index is None:
        return {}
    if hasattr(project_index, "to_dict"):
        raw = project_index.to_dict()
    elif isinstance(project_index, dict):
        raw = project_index
    else:
        return {}
    return {
        "project_key": raw.get("project_key", ""),
        "file_count": int(raw.get("file_count") or 0),
        "git_commit": str(raw.get("git_commit") or "")[:12],
        "entrypoints": list(raw.get("entrypoints") or [])[:MAX_RUNTIME_STATE_WS_ITEMS],
        "dir_tree": list(raw.get("dir_tree") or [])[:8],
    }


def _summarize_working_set(working_set: Any | None) -> dict[str, Any]:
    if working_set is None:
        return {}
    if hasattr(working_set, "to_dict"):
        raw = working_set.to_dict()
    elif isinstance(working_set, dict):
        raw = working_set
    else:
        return {}
    return {
        "priority_targets": list(raw.get("priority_targets") or [])[:MAX_RUNTIME_STATE_WS_ITEMS],
        "must_include": list(raw.get("must_include") or [])[:MAX_RUNTIME_STATE_WS_ITEMS],
        "retrieved_token_cap": int(raw.get("retrieved_token_cap") or 0),
        "project_index_used": bool(raw.get("project_index_used")),
    }


def _summarize_anchors(state: Any, *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for a in list(getattr(state, "evidence_anchors", None) or [])[-limit:]:
        if not isinstance(a, dict):
            continue
        rows.append({
            "path": a.get("path", ""),
            "symbol": a.get("symbol", ""),
            "line_start": a.get("line_start", 0),
            "line_end": a.get("line_end", 0),
            "summary": str(a.get("summary") or "")[:200],
            "quality": a.get("evidence_quality", 0),
        })
    return rows


def _summarize_handoff(state: Any) -> dict[str, Any]:
    ho = dict(getattr(state, "handoff", None) or {})
    if not ho:
        return {}
    return {
        "updated_at": ho.get("updated_at", ""),
        "query": str(ho.get("query") or "")[:300],
        "phase_hint": ho.get("phase_hint", ""),
        "files_read": list(ho.get("files_read") or [])[:8],
        "evidence_collected": list(ho.get("evidence_collected") or [])[:8],
        "remaining_risks": list(ho.get("remaining_risks") or [])[:6],
        "anchor_count": int(ho.get("anchor_count") or 0),
        "journal_count": int(ho.get("journal_count") or 0),
    }


def _last_tool_results(state: Any, *, limit: int = 6) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for j in list(getattr(state, "task_journal", None) or [])[-limit:]:
        if not isinstance(j, dict):
            continue
        if j.get("kind") not in ("read", "grep", "glob", "edit", "shell", "tool", "failure"):
            continue
        rows.append({
            "kind": j.get("kind"),
            "target": j.get("target", ""),
            "summary": str(j.get("summary") or "")[:160],
            "success": (j.get("meta") or {}).get("success", j.get("kind") != "failure"),
        })
    return rows


def _available_actions(ap: dict[str, Any], phase: str) -> list[str]:
    base = ["read", "grep", "glob", "shell", "edit", "summarize", "final", "ask_user", "recover"]
    allowed = {str(t).lower() for t in (ap.get("allowed_tools") or [])}
    banned = {str(t).lower() for t in (ap.get("banned_tools") or [])}
    if phase in ("final_answer", "partial_final_answer", "recovery_final"):
        preferred = ["final", "summarize", "ask_user"]
    elif ap.get("final_ready"):
        preferred = ["final", "read", "grep", "recover"]
    else:
        preferred = ["read", "grep", "glob", "shell", "edit", "recover", "summarize"]
    out: list[str] = []
    for a in preferred + base:
        if a in out:
            continue
        if banned and a in banned:
            continue
        if allowed and a not in allowed and a not in ("final", "ask_user", "recover", "summarize"):
            tool_map = {"read": "read", "grep": "grep", "glob": "glob", "shell": "shell", "edit": "write"}
            if tool_map.get(a, a) not in allowed:
                continue
        out.append(a)
    return out[:12]


class RuntimeStateBuilder:
    """Collect kernel memory artifacts into planner RuntimeState."""

    def __init__(self, limits: RuntimeStateLimits | None = None) -> None:
        self.limits = limits or RuntimeStateLimits()

    def build(
        self,
        *,
        session_state: Any,
        query: str = "",
        phase: str = "tool_planning",
        router_intent: str = "",
        context_intent: str = "",
        project_index: Any | None = None,
        working_set: Any | None = None,
        budget_plan: Any | None = None,
        coverage: Any | None = None,
        context_need: Any | None = None,
    ) -> RuntimeState:
        ap = dict(getattr(session_state, "agent_plan", None) or {})
        q = query or getattr(session_state, "current_query", "") or ""

        if project_index is None:
            pi_raw = getattr(session_state, "project_index", None)
            if isinstance(pi_raw, dict) and pi_raw:
                project_index = pi_raw
            else:
                from runtime_kernel.project_index import ProjectIndex

                project_index = ProjectIndex.from_dict(pi_raw)
        if working_set is None:
            ws_raw = getattr(session_state, "last_working_set", None)
            working_set = ws_raw

        budget: dict[str, Any] = {}
        if budget_plan is not None:
            budget = budget_plan.to_dict() if hasattr(budget_plan, "to_dict") else dict(budget_plan or {})

        cov: dict[str, Any] = {}
        if coverage is not None:
            cov = coverage.to_dict() if hasattr(coverage, "to_dict") else dict(coverage or {})

        need_dict: dict[str, Any] = {}
        if context_need is not None:
            need_dict = context_need.to_dict() if hasattr(context_need, "to_dict") else dict(context_need or {})

        journal = list(getattr(session_state, "task_journal", None) or [])[-self.limits.max_journal_items :]
        anchors = _summarize_anchors(session_state, limit=self.limits.max_anchor_items)

        constraints = {
            "avoid_actions": list(ap.get("avoid_actions") or [])[:8],
            "banned_tools": list(ap.get("banned_tools") or [])[:8],
            "missing_evidence": list(ap.get("missing_evidence") or getattr(session_state, "missing_evidence", None) or [])[:8],
            "evidence_needed": list(ap.get("evidence_needed") or [])[:12],
            "evidence_collected": list(ap.get("evidence_collected") or [])[:12],
            "final_ready": bool(ap.get("final_ready")),
            "coverage_complete": bool(cov.get("complete", True)),
        }

        rs = RuntimeState(
            current_user_request=q[:2000],
            router_intent=router_intent or str(ap.get("router_intent") or ap.get("task_intent") or ""),
            context_intent=context_intent or str(need_dict.get("intent") or router_intent or ""),
            phase=phase,
            project_index_summary=_summarize_project_index(project_index),
            working_set_summary=_summarize_working_set(working_set),
            task_journal_tail=journal,
            evidence_anchor_summary=anchors,
            handoff_summary=_summarize_handoff(session_state),
            runtime_self_model=load_self_model(),
            budget_state=budget,
            coverage_state=cov,
            last_tool_results=_last_tool_results(session_state),
            constraints=constraints,
            available_actions=_available_actions(ap, phase),
            raw={
                "agent_plan_excerpt": {
                    "goal": str(ap.get("goal") or "")[:400],
                    "next_action": dict(ap.get("next_action") or {}),
                    "confidence": float(ap.get("confidence") or 0),
                },
                "context_need": need_dict,
                "files_read": list(getattr(session_state, "files_read", None) or [])[-20:],
            },
        )
        return rs


def persist_planner_runtime_state(session_state: Any, runtime_state: RuntimeState) -> None:
    if session_state is None:
        return
    session_state.planner_runtime_state = runtime_state.compact_dict()
    session_state.planner_runtime_state_raw = dict(runtime_state.raw)
    session_state.planner_runtime_state_prompt = runtime_state.to_prompt_json()
