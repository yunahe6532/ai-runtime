"""Runtime Kernel — unified intent resolution (replaces router/context/task triple)."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .constants import DEFAULT_PACK_BUDGET

LOG = logging.getLogger("runtime_kernel.intent")


class BudgetProfile(str, Enum):
    """Maps to ContextNeed INTENT_PRESETS."""
    BUGFIX = "bugfix"
    CODE_EDIT = "code_edit"
    RECALL = "recall"
    DOC_SUMMARY = "doc_summary"
    ARCHITECTURE = "architecture"
    GENERAL = "general"


class EvidenceProfile(str, Enum):
    """Maps to planner DEFAULT_EVIDENCE task templates."""
    GENERAL = "general"
    PROJECT_INSPECTION = "project_inspection"
    BENCHMARK_ANALYSIS = "benchmark_analysis"
    LOG_ANALYSIS = "log_analysis"
    RUNTIME_DIAGNOSIS = "runtime_diagnosis"
    FLOW_ANALYSIS = "flow_analysis"
    CODE_ANALYSIS = "code_analysis"


class RuntimeIntentName(str, Enum):
    CASUAL = "casual"
    EXPLAIN = "explain"
    CODE_EDIT = "code_edit"
    SHELL_TASK = "shell_task"
    LOG_ANALYSIS = "log_analysis"
    BENCHMARK = "benchmark"
    DEBUG = "debug"
    CONTINUE_PREVIOUS = "continue_previous"
    READ_ONLY_ANALYSIS = "read_only_analysis"
    PROJECT_INSPECTION = "project_inspection"
    AGENT = "agent"


READ_ONLY_KW = (
    "코드 수정 말고", "수정하지 말", "수정하지말", "읽어서", "파일만",
    "구조 분석", "프로젝트 구조", "역할을 요약", "역할 요약", "근거와 함께",
    "read only", "without modifying",
)
ANALYSIS_KW = (
    "분석", "문제점", "요약", "검증 결과", "어떤 구조", "어떻게 처리",
    "상세분석", "맞는지", "설명해", "구조 파악", "로그 분석",
)
CODE_EDIT_STRONG_KW = (
    "수정해", "패치해", "구현해", "파일 바꿔", "코드 작성", "apply",
    "리팩토", "커밋", "pr ", "pull request",
)
RECALL_KW = (
    "기억", "아까", "이전", "전에", "뭐 이야기", "remember", "recall", "earlier", "what did we",
)
BUGFIX_KW = (
    "버그", "bug", "fix", "수정", "오류", "error", "broken", "fail",
)
STRUCTURE_KW = (
    "구조", "structure", "architecture", "arch", "아키텍처", "역할",
    "layout", "codebase", "directory", "component", "components",
    "module", "modules", "package", "packages",
)
DOC_SUMMARY_KW = (
    "요약", "summarize", "summary", "논문", "paper", "document", "문서 분석",
)

EVIDENCE_BY_INTENT: dict[str, EvidenceProfile] = {
    RuntimeIntentName.PROJECT_INSPECTION.value: EvidenceProfile.PROJECT_INSPECTION,
    RuntimeIntentName.READ_ONLY_ANALYSIS.value: EvidenceProfile.PROJECT_INSPECTION,
    RuntimeIntentName.BENCHMARK.value: EvidenceProfile.BENCHMARK_ANALYSIS,
    RuntimeIntentName.LOG_ANALYSIS.value: EvidenceProfile.LOG_ANALYSIS,
    RuntimeIntentName.SHELL_TASK.value: EvidenceProfile.GENERAL,
    RuntimeIntentName.CODE_EDIT.value: EvidenceProfile.CODE_ANALYSIS,
    RuntimeIntentName.AGENT.value: EvidenceProfile.GENERAL,
}


@dataclass
class RuntimeIntentResolution:
    """Single intent object consumed by Kernel, Brain, and Observability."""

    name: str
    budget_profile: str
    evidence_profile: str
    route: str  # fast | main | long
    needs_tools: bool
    needs_files: bool
    needs_shell: bool
    needs_prior_summary: bool
    needs_raw_tool_results: bool
    needs_full_raw_context: bool
    context_budget_tokens: int
    context_pack: list[str]
    reason: str = ""
    score: float = 0.0
    file_ref_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RuntimeIntentResolution | None:
        if not data or not data.get("name"):
            return None
        fields = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**fields)

    def legacy_router_intent(self) -> str:
        return self.name

    def legacy_context_intent(self) -> str:
        return self.budget_profile

    def legacy_task_intent(self) -> str:
        return self.evidence_profile if self.evidence_profile != "general" else "general"

    def to_intent_result(self) -> Any:
        """Map to legacy IntentResult without circular import at module load."""
        from intent_router import IntentResult

        return IntentResult(
            intent=self.name,
            route=self.route,
            needs_tools=self.needs_tools,
            needs_files=self.needs_files,
            needs_shell=self.needs_shell,
            needs_prior_summary=self.needs_prior_summary,
            needs_raw_tool_results=self.needs_raw_tool_results,
            needs_full_raw_context=self.needs_full_raw_context,
            context_budget_tokens=self.context_budget_tokens,
            context_pack=list(self.context_pack),
            reason=self.reason,
        )


def _match_any(text: str, patterns: tuple[str, ...] | list[str]) -> bool:
    lower = text.lower()
    return any(p in lower or p in text for p in patterns)


def _score_keywords(text: str, patterns: list[str], weight: float = 2.0) -> float:
    lower = text.lower()
    return sum(weight for p in patterns if p in lower or p in text)


def _count_file_refs(text: str) -> int:
    return len(re.findall(r"[\w./~-]+\.(?:py|yml|yaml|sh|json|md|txt)", text, re.I))


def _is_read_only_analysis(q: str) -> bool:
    if _has_strong_code_edit_signal(q):
        return False
    hits = sum(1 for kw in READ_ONLY_KW if kw in q)
    if "코드 수정 말고" in q or "수정하지 말" in q:
        return True
    if hits >= 2 and _is_analysis_query(q):
        return True
    return hits >= 3


def _is_analysis_query(q: str) -> bool:
    ql = q.lower()
    return any(kw in q for kw in ANALYSIS_KW) or any(kw in ql for kw in ("analyze", "analysis"))


def _has_strong_code_edit_signal(q: str) -> bool:
    ql = q.lower()
    return any(kw in q or kw in ql for kw in CODE_EDIT_STRONG_KW)


def _derive_budget_profile(name: str, query: str) -> str:
    q = (query or "").lower()
    if any(k in q for k in RECALL_KW):
        return BudgetProfile.RECALL.value
    if name in (RuntimeIntentName.READ_ONLY_ANALYSIS.value, RuntimeIntentName.PROJECT_INSPECTION.value):
        return BudgetProfile.ARCHITECTURE.value
    if any(k in q for k in STRUCTURE_KW) and _is_analysis_query(query):
        return BudgetProfile.ARCHITECTURE.value
    if any(k in q for k in DOC_SUMMARY_KW):
        return BudgetProfile.DOC_SUMMARY.value
    if any(k in q for k in BUGFIX_KW) or name in (
        RuntimeIntentName.CODE_EDIT.value,
        RuntimeIntentName.DEBUG.value,
        RuntimeIntentName.SHELL_TASK.value,
    ):
        return BudgetProfile.BUGFIX.value
    if name in (RuntimeIntentName.CODE_EDIT.value, RuntimeIntentName.BENCHMARK.value, RuntimeIntentName.AGENT.value):
        return BudgetProfile.CODE_EDIT.value
    if name in (RuntimeIntentName.EXPLAIN.value, RuntimeIntentName.CASUAL.value):
        return BudgetProfile.GENERAL.value
    return BudgetProfile.GENERAL.value


def _derive_evidence_profile(name: str) -> str:
    return EVIDENCE_BY_INTENT.get(name, EvidenceProfile.GENERAL).value


def _intent_flags(name: str) -> dict[str, Any]:
    """Router-level flags formerly scattered in intent_router._intent_from_name."""
    if name == RuntimeIntentName.BENCHMARK.value:
        return dict(
            route="main", needs_tools=True, needs_files=True, needs_shell=True,
            needs_prior_summary=True, needs_raw_tool_results=False, needs_full_raw_context=False,
            context_budget_tokens=DEFAULT_PACK_BUDGET,
            context_pack=["current_query", "recent_files", "benchmark_script_refs", "recent_router_logs", "rules"],
        )
    if name == RuntimeIntentName.LOG_ANALYSIS.value:
        return dict(
            route="main", needs_tools=True, needs_files=False, needs_shell=True,
            needs_prior_summary=False, needs_raw_tool_results=True, needs_full_raw_context=False,
            context_budget_tokens=DEFAULT_PACK_BUDGET,
            context_pack=["current_query", "recent_router_logs", "rules"],
        )
    if name == RuntimeIntentName.SHELL_TASK.value:
        return dict(
            route="main", needs_tools=True, needs_files=True, needs_shell=True,
            needs_prior_summary=False, needs_raw_tool_results=False, needs_full_raw_context=False,
            context_budget_tokens=DEFAULT_PACK_BUDGET,
            context_pack=["current_query", "recent_files", "rules"],
        )
    if name == RuntimeIntentName.DEBUG.value:
        return dict(
            route="main", needs_tools=False, needs_files=False, needs_shell=False,
            needs_prior_summary=False, needs_raw_tool_results=False, needs_full_raw_context=False,
            context_budget_tokens=6000,
            context_pack=["current_query", "project_state_summary", "rules"],
        )
    if name == RuntimeIntentName.CODE_EDIT.value:
        return dict(
            route="main", needs_tools=True, needs_files=True, needs_shell=True,
            needs_prior_summary=True, needs_raw_tool_results=False, needs_full_raw_context=False,
            context_budget_tokens=DEFAULT_PACK_BUDGET,
            context_pack=["current_query", "project_state_summary", "recent_files", "rules"],
        )
    if name == RuntimeIntentName.CONTINUE_PREVIOUS.value:
        return dict(
            route="main", needs_tools=True, needs_files=True, needs_shell=False,
            needs_prior_summary=True, needs_raw_tool_results=False, needs_full_raw_context=False,
            context_budget_tokens=DEFAULT_PACK_BUDGET,
            context_pack=["current_query", "project_state_summary", "recent_files", "rules"],
        )
    if name == RuntimeIntentName.EXPLAIN.value:
        return dict(
            route="main", needs_tools=False, needs_files=False, needs_shell=False,
            needs_prior_summary=True, needs_raw_tool_results=False, needs_full_raw_context=False,
            context_budget_tokens=8000,
            context_pack=["current_query", "project_state_summary", "rules"],
        )
    if name == RuntimeIntentName.PROJECT_INSPECTION.value:
        return dict(
            route="main", needs_tools=False, needs_files=False, needs_shell=False,
            needs_prior_summary=True, needs_raw_tool_results=False, needs_full_raw_context=False,
            context_budget_tokens=8000,
            context_pack=["current_query", "project_state_summary", "rules"],
        )
    if name == RuntimeIntentName.READ_ONLY_ANALYSIS.value:
        return dict(
            route="main", needs_tools=True, needs_files=True, needs_shell=False,
            needs_prior_summary=True, needs_raw_tool_results=False, needs_full_raw_context=False,
            context_budget_tokens=8000,
            context_pack=["current_query", "project_state_summary", "recent_files", "rules"],
        )
    if name == RuntimeIntentName.CASUAL.value:
        return dict(
            route="fast", needs_tools=False, needs_files=False, needs_shell=False,
            needs_prior_summary=False, needs_raw_tool_results=False, needs_full_raw_context=False,
            context_budget_tokens=2000,
            context_pack=["current_query"],
        )
    return dict(
        route="main", needs_tools=True, needs_files=True, needs_shell=False,
        needs_prior_summary=True, needs_raw_tool_results=False, needs_full_raw_context=False,
        context_budget_tokens=DEFAULT_PACK_BUDGET,
        context_pack=["current_query", "project_state_summary", "recent_files", "tool_result_refs", "rules"],
    )


def resolve_runtime_intent(query: str, *, file_ref_count: int | None = None) -> RuntimeIntentResolution:
    """SSOT intent resolver — replaces classify_intent + resolve_context_intent + task_intent guess."""
    q = (query or "").strip()
    is_analysis = _is_analysis_query(q)
    has_strong_edit = _has_strong_code_edit_signal(q)
    file_refs = file_ref_count if file_ref_count is not None else _count_file_refs(q)

    scores: dict[str, float] = {
        RuntimeIntentName.BENCHMARK.value: _score_keywords(q, ["벤치", "benchmark", "성능 측정", "tok/s", "속도 측정", "벤치마킹"]),
        RuntimeIntentName.LOG_ANALYSIS.value: _score_keywords(q, ["docker logs", "서버로그", "server log"])
        + _score_keywords(q, ["로그", "log"], 1.0),
        RuntimeIntentName.SHELL_TASK.value: _score_keywords(q, ["실행", "bash", "docker compose", "shell", "grep"]),
        RuntimeIntentName.DEBUG.value: _score_keywords(q, ["복붙", "다시 뱉", "반복", "같은 질문", "이전 답"]),
        RuntimeIntentName.CODE_EDIT.value: _score_keywords(q, ["수정", "구현", "패치", "fix", "implement", "refactor", "코드", "파일"]),
        RuntimeIntentName.CONTINUE_PREVIOUS.value: _score_keywords(
            q, ["cut off", "exceeded the output token", "continue from where you left off", "이어서", "잘렸", "이어", "계속", "continue"],
        ),
        RuntimeIntentName.EXPLAIN.value: _score_keywords(q, ["설명", "어떻게", "왜", "what is", "explain", "알려줘"]),
        RuntimeIntentName.PROJECT_INSPECTION.value: 0.0,
        RuntimeIntentName.READ_ONLY_ANALYSIS.value: 0.0,
    }

    if _is_read_only_analysis(q):
        scores[RuntimeIntentName.READ_ONLY_ANALYSIS.value] += 10.0
        scores[RuntimeIntentName.EXPLAIN.value] += 2.0
        scores[RuntimeIntentName.CODE_EDIT.value] = max(0.0, scores[RuntimeIntentName.CODE_EDIT.value] - 4.0)

    if is_analysis:
        scores[RuntimeIntentName.EXPLAIN.value] += 3.0
        if "로그" in q or "log" in q.lower():
            scores[RuntimeIntentName.LOG_ANALYSIS.value] += 3.0
        try:
            from reference.evidence_extractors import looks_like_project_inspection

            if looks_like_project_inspection(q):
                if _is_read_only_analysis(q):
                    scores[RuntimeIntentName.READ_ONLY_ANALYSIS.value] += 2.0
                else:
                    scores[RuntimeIntentName.PROJECT_INSPECTION.value] += 4.0
        except ImportError:
            pass

    if has_strong_edit:
        scores[RuntimeIntentName.CODE_EDIT.value] += 5.0

    if file_refs >= 3 and not is_analysis:
        scores[RuntimeIntentName.CODE_EDIT.value] += 6.0
    elif file_refs >= 1 and not is_analysis:
        scores[RuntimeIntentName.CODE_EDIT.value] += 3.0
    elif file_refs >= 1 and is_analysis:
        scores[RuntimeIntentName.PROJECT_INSPECTION.value] += 1.5

    has_docker_exec = any(w in q.lower() for w in ["docker ps", "docker logs", "container", "docker exec"])
    if has_docker_exec or (
        any(w in q for w in ["진단", "동작하는지", "정상", "status", "확인해", "호출"])
        and "docker" in q.lower()
        and not (file_refs >= 1 and any(w in q for w in ["읽", "read", "확인", "검증"]))
    ):
        scores[RuntimeIntentName.SHELL_TASK.value] += 4.0

    needs_shell_hint = "curl" in q.lower() or "테스트" in q or "실행" in q or has_docker_exec

    best = max(scores, key=lambda n: scores[n])
    best_score = scores[best]

    if best_score < 2.0:
        if len(q) <= 200 and not _match_any(q, [
            "스크립트", "script", "서버", "분석", "확인", "짜", "작성", "수정",
            "구현", "파일", "코드", "docker", "grep", "benchmark", "벤치",
        ]):
            best = RuntimeIntentName.CASUAL.value
            best_score = 2.0
            reason = "short casual question"
        else:
            best = RuntimeIntentName.AGENT.value
            reason = "default agent task"
    else:
        reason = f"score={best_score:.1f} files={file_refs}"

    flags = _intent_flags(best)
    if needs_shell_hint and best != RuntimeIntentName.CASUAL.value:
        flags["needs_shell"] = True

    resolution = RuntimeIntentResolution(
        name=best,
        budget_profile=_derive_budget_profile(best, q),
        evidence_profile=_derive_evidence_profile(best),
        reason=reason,
        score=best_score,
        file_ref_count=file_refs,
        **flags,
    )
    LOG.info(
        "runtime_intent name=%s budget=%s evidence=%s route=%s reason=%s",
        resolution.name,
        resolution.budget_profile,
        resolution.evidence_profile,
        resolution.route,
        resolution.reason,
    )
    return resolution
