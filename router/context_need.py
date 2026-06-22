"""ContextNeed — Planner declares what information this turn requires."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FILE_REF_RE = re.compile(
    r"[\w./-]+\.(?:py|md|ts|tsx|js|json|yaml|yml|sh|go|rs|txt)",
    re.IGNORECASE,
)
QUERY_SYMBOL_RE = re.compile(
    r"([\w./-]+\.(?:py|md|ts|tsx|js|json|yaml|yml|sh|go|rs))::([\w_]+)",
    re.IGNORECASE,
)

RECALL_KW = (
    "기억", "아까", "이전", "전에", "뭐 이야기", "remember", "recall", "earlier", "what did we",
)
DOC_SUMMARY_KW = (
    "요약", "summarize", "summary", "논문", "paper", "document", "문서 분석",
)
BUGFIX_KW = (
    "버그", "bug", "fix", "수정", "오류", "error", "broken", "fail",
)
STRUCTURE_KW = (
    "구조",
    "structure",
    "architecture",
    "arch",
    "아키텍처",
    "역할",
    "layout",
    "codebase",
    "directory",
    "component",
    "components",
    "module",
    "modules",
    "package",
    "packages",
)


@dataclass
class ContextNeed:
    intent: str = "general"
    required_sources: list[str] = field(default_factory=list)
    priority: dict[str, float] = field(default_factory=dict)
    must_include: list[str] = field(default_factory=list)
    coverage_targets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ContextNeed:
        if not data:
            return cls()
        fields = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**fields)


def _preset(
    intent: str,
    sources: list[str],
    priority: dict[str, float],
    must_include: list[str],
    coverage_targets: list[str],
) -> ContextNeed:
    return ContextNeed(
        intent=intent,
        required_sources=sources,
        priority=priority,
        must_include=must_include,
        coverage_targets=coverage_targets,
    )


# Five intent presets (Phase 1 dynamic budget)
INTENT_PRESETS: dict[str, ContextNeed] = {
    "bugfix": _preset(
        "bugfix",
        ["retrieved_code", "artifact", "tool_result", "session"],
        {
            "current_task": 0.05,
            "retrieved": 0.45,
            "artifact": 0.35,
            "session_tail": 0.05,
            "state": 0.05,
            "long_memory": 0.05,
        },
        ["current user request", "latest tool result", "active agent plan"],
        [
            "context_budget.py",
            "prompt_builder.py",
            "planner.py::normalize_plan",
            "context_budget.py::allocate_dynamic",
            "prompt_builder.py::build_with_budget",
        ],
    ),
    "code_edit": _preset(
        "code_edit",
        ["retrieved_code", "artifact", "tool_result"],
        {
            "current_task": 0.08,
            "retrieved": 0.40,
            "artifact": 0.35,
            "session_tail": 0.05,
            "state": 0.07,
            "long_memory": 0.05,
        },
        ["current user request", "active agent plan", "latest tool result"],
        ["planner.py", "prompt_builder.py", "legacy/memory_store.py"],
    ),
    "recall": _preset(
        "recall",
        ["session", "long_memory", "artifact"],
        {
            "current_task": 0.05,
            "session_tail": 0.55,
            "long_memory": 0.25,
            "retrieved": 0.05,
            "state": 0.10,
        },
        ["current user request", "session context", "active agent plan"],
        ["previous decision", "agent plan", "session_state"],
    ),
    "doc_summary": _preset(
        "doc_summary",
        ["retrieved_code", "artifact"],
        {
            "current_task": 0.05,
            "retrieved": 0.70,
            "artifact": 0.15,
            "state": 0.05,
            "session_tail": 0.05,
        },
        ["current user request", "document content"],
        ["document", "summary", "section"],
    ),
    "architecture": _preset(
        "architecture",
        ["retrieved_code", "artifact", "session"],
        {
            "current_task": 0.06,
            "retrieved": 0.35,
            "artifact": 0.20,
            "state": 0.15,
            "session_tail": 0.14,
            "long_memory": 0.10,
        },
        ["current user request", "active agent plan"],
        [],
    ),
    "general": _preset(
        "general",
        ["session", "artifact", "retrieved_code", "tool_result"],
        {
            "current_task": 0.20,
            "retrieved": 0.20,
            "session_tail": 0.20,
            "state": 0.15,
            "artifact": 0.15,
            "long_memory": 0.10,
        },
        ["current user request", "active agent plan"],
        [],
    ),
}

TASK_INTENT_MAP: dict[str, str] = {
    "benchmark_analysis": "code_edit",
    "code_analysis": "code_edit",
    "runtime_diagnosis": "architecture",
    "project_inspection": "architecture",
    "log_analysis": "doc_summary",
    "flow_analysis": "doc_summary",
    "html_validation": "bugfix",
    "compose_port": "bugfix",
}


def _looks_like_structure_analysis(
    query: str,
    task_intent: str,
    router_intent: str,
) -> bool:
    q = (query or "").lower()
    if router_intent == "read_only_analysis" or task_intent == "project_inspection":
        return True
    try:
        from reference.target_coverage import looks_like_read_only_query

        if looks_like_read_only_query(query):
            return True
    except ImportError:
        pass
    if any(k in q for k in STRUCTURE_KW):
        return True
    return False


def resolve_context_intent(
    task_intent: str,
    query: str,
    router_intent: str = "",
) -> str:
    q = (query or "").lower()
    if any(k in q for k in RECALL_KW):
        return "recall"
    if _looks_like_structure_analysis(query, task_intent, router_intent):
        return "architecture"
    if any(k in q for k in DOC_SUMMARY_KW):
        return "doc_summary"
    if any(k in q for k in BUGFIX_KW) or router_intent in ("code_edit", "debug", "shell_task"):
        return "bugfix"
    if task_intent in TASK_INTENT_MAP:
        return TASK_INTENT_MAP[task_intent]
    if router_intent in ("code_edit", "benchmark", "agent"):
        return "code_edit"
    if router_intent in ("explain", "casual"):
        return "general"
    return "general"


def extract_query_coverage_targets(query: str) -> list[str]:
    """Pull file/symbol targets explicitly named in the user query."""
    q = query or ""
    targets: list[str] = []
    for m in QUERY_SYMBOL_RE.finditer(q):
        path = Path(m.group(1)).name
        targets.append(f"{path}::{m.group(2)}")
    for raw in FILE_REF_RE.findall(q):
        name = Path(raw).name
        if name and name not in targets:
            targets.append(name)
    words = q.replace(",", " ").replace(";", " ").split()
    for i, word in enumerate(words):
        if not FILE_REF_RE.fullmatch(word):
            continue
        fname = Path(word).name
        if i + 1 < len(words):
            sym = words[i + 1].strip(".,")
            if sym.isidentifier() and sym not in {"bug", "fix", "error", "the", "in", "and"}:
                sym_target = f"{fname}::{sym}"
                if sym_target not in targets:
                    targets.append(sym_target)
    return list(dict.fromkeys(targets))


def refine_coverage_targets(
    query: str,
    preset_targets: list[str],
    intent: str,
    *,
    known_files: list[str] | None = None,
) -> list[str]:
    """Narrow preset targets to query-relevant working set (keeps compression honest)."""
    extracted = extract_query_coverage_targets(query)
    for path in known_files or []:
        name = Path(str(path)).name
        if name and name not in extracted:
            extracted.append(name)
    if extracted:
        out = list(extracted)
        file_names = {t.split("::")[0] for t in extracted if "::" in t}
        file_names.update(t for t in extracted if "::" not in t)
        for pt in preset_targets or []:
            pt_str = str(pt)
            if "::" in pt_str:
                fp = pt_str.split("::", 1)[0]
                if Path(fp).name in file_names and pt_str not in out:
                    out.append(pt_str)
            elif Path(pt_str).name in file_names and pt_str not in out:
                out.append(pt_str)
        return out[:8]

    if intent in ("recall", "doc_summary", "architecture") and preset_targets:
        return list(preset_targets)

    if intent == "bugfix" and len(preset_targets or []) > 2:
        files: list[str] = []
        symbols: list[str] = []
        for t in preset_targets:
            if "::" in str(t):
                symbols.append(str(t))
            else:
                files.append(str(t))
        return (files[:2] + symbols[:2])[:4]

    return list(preset_targets or [])


def build_context_need(
    agent_plan: Any,
    query: str,
    router_intent: str = "",
    phase: str = "tool_planning",
) -> ContextNeed:
    """Derive ContextNeed from AgentPlan + unified runtime intent."""
    runtime_intent: dict[str, Any] = {}
    if isinstance(agent_plan, dict):
        runtime_intent = dict(agent_plan.get("runtime_intent") or {})
    elif hasattr(agent_plan, "to_dict"):
        runtime_intent = dict((agent_plan.to_dict() or {}).get("runtime_intent") or {})

    budget_profile = str(runtime_intent.get("budget_profile") or "").strip()
    if budget_profile and budget_profile in INTENT_PRESETS:
        preset = INTENT_PRESETS[budget_profile]
        ctx_intent = budget_profile
    else:
        task_intent = getattr(agent_plan, "task_intent", None) or (
            agent_plan.get("task_intent") if isinstance(agent_plan, dict) else "general"
        )
        ctx_intent = resolve_context_intent(str(task_intent or ""), query, router_intent)
        preset = INTENT_PRESETS.get(ctx_intent, INTENT_PRESETS["general"])

    known_files: list[str] = []
    if hasattr(agent_plan, "known_files"):
        known_files = list(agent_plan.known_files or [])
    elif isinstance(agent_plan, dict):
        known_files = list(agent_plan.get("known_files") or [])

    need = ContextNeed(
        intent=ctx_intent,
        required_sources=list(preset.required_sources),
        priority=dict(preset.priority),
        must_include=list(preset.must_include),
        coverage_targets=list(preset.coverage_targets),
    )

    preferred_sources: list[str] = []
    if hasattr(agent_plan, "preferred_sources"):
        preferred_sources = list(agent_plan.preferred_sources or [])
    elif isinstance(agent_plan, dict):
        preferred_sources = list(agent_plan.get("preferred_sources") or [])
    if preferred_sources:
        need.coverage_targets = list(dict.fromkeys(preferred_sources))[:12]
    else:
        for path in known_files[:8]:
            name = Path(path).name
            if name and name not in need.coverage_targets:
                need.coverage_targets.append(name)
        need.coverage_targets = refine_coverage_targets(
            query, need.coverage_targets, need.intent, known_files=known_files,
        )

    if phase == "tool_planning" and "latest tool result" not in need.must_include:
        need.must_include.append("latest tool result")

    next_action = getattr(agent_plan, "next_action", None) or (
        agent_plan.get("next_action") if isinstance(agent_plan, dict) else {}
    )
    if isinstance(next_action, dict) and not preferred_sources:
        target = str(next_action.get("target") or "")
        if target:
            tname = Path(target).name
            if tname and tname not in need.coverage_targets:
                need.coverage_targets.append(tname)

    return need


def extract_context_need(
    agent_plan: Any,
    query: str,
    router_intent: str = "",
    phase: str = "tool_planning",
) -> ContextNeed:
    """Public alias used by dynamic_context_scheduler."""
    rule = build_context_need(agent_plan, query, router_intent, phase)
    stored: dict[str, Any] | None = None
    if hasattr(agent_plan, "context_need") and agent_plan.context_need:
        raw = agent_plan.context_need
        if isinstance(raw, dict) and raw.get("intent"):
            stored = raw
    elif isinstance(agent_plan, dict) and agent_plan.get("context_need"):
        raw = agent_plan["context_need"]
        if isinstance(raw, dict) and raw.get("intent"):
            stored = raw
    if stored:
        need = merge_context_need(stored, rule)
        preferred_sources: list[str] = []
        required_ids: list[str] = []
        if hasattr(agent_plan, "preferred_sources"):
            preferred_sources = list(agent_plan.preferred_sources or [])
            required_ids = list(agent_plan.required_source_ids or [])
        elif isinstance(agent_plan, dict):
            preferred_sources = list(agent_plan.get("preferred_sources") or [])
            required_ids = list(agent_plan.get("required_source_ids") or [])
        if required_ids:
            need.coverage_targets = list(dict.fromkeys(required_ids))[:12]
        elif preferred_sources:
            need.coverage_targets = list(dict.fromkeys(preferred_sources))[:12]
        else:
            merged_targets = list(dict.fromkeys(list(rule.coverage_targets)))
            if stored.get("coverage_targets"):
                merged_targets = list(
                    dict.fromkeys(merged_targets + list(stored.get("coverage_targets") or []))
                )
            need.coverage_targets = merged_targets[:12]
    else:
        need = rule
    if phase == "tool_planning" and "latest tool result" not in need.must_include:
        need.must_include.append("latest tool result")
    return need


VALID_CONTEXT_INTENTS = frozenset({
    "bugfix", "recall", "doc_summary", "architecture", "code_edit", "general",
})
VALID_REQUIRED_SOURCES = frozenset({
    "session", "artifact", "retrieved_code", "tool_result", "long_memory", "latest_tool_result",
})


def validate_context_need(data: dict[str, Any] | None) -> tuple[ContextNeed | None, list[str]]:
    """Schema validation for LLM-produced context_need."""
    if not isinstance(data, dict) or not data:
        return None, ["empty"]
    errors: list[str] = []
    intent = str(data.get("intent") or "")
    if intent and intent not in VALID_CONTEXT_INTENTS:
        errors.append(f"invalid_intent:{intent}")
    sources = data.get("required_sources") or []
    if not isinstance(sources, list):
        errors.append("required_sources_not_list")
        sources = []
    for s in sources:
        if str(s) not in VALID_REQUIRED_SOURCES:
            errors.append(f"invalid_source:{s}")
    priority = data.get("priority") or {}
    if priority and not isinstance(priority, dict):
        errors.append("priority_not_dict")
        priority = {}
    must = data.get("must_include") or []
    if must and not isinstance(must, list):
        errors.append("must_include_not_list")
        must = []
    targets = data.get("coverage_targets") or []
    if targets and not isinstance(targets, list):
        errors.append("coverage_targets_not_list")
        targets = []
    if errors:
        return None, errors
    return ContextNeed(
        intent=intent or "general",
        required_sources=[str(s) for s in sources],
        priority={str(k): float(v) for k, v in priority.items()},
        must_include=[str(m) for m in must],
        coverage_targets=[str(t) for t in targets],
    ), []


def merge_context_need(llm_data: dict[str, Any] | None, rule_need: ContextNeed) -> ContextNeed:
    """LLM context_need → validate → merge with rule fallback."""
    if not llm_data:
        return rule_need
    validated, errors = validate_context_need(llm_data)
    if validated is None:
        return rule_need
    merged = ContextNeed(
        intent=validated.intent if validated.intent in VALID_CONTEXT_INTENTS else rule_need.intent,
        required_sources=list(dict.fromkeys(
            (validated.required_sources or []) + (rule_need.required_sources or [])
        )),
        priority={**(rule_need.priority or {}), **(validated.priority or {})},
        must_include=list(dict.fromkeys(
            (rule_need.must_include or []) + (validated.must_include or [])
        )),
        coverage_targets=list(dict.fromkeys(
            (rule_need.coverage_targets or []) + (validated.coverage_targets or [])
        )),
    )
    if errors:
        merged.priority = dict(rule_need.priority or {})
    return merged
