"""2-pass router: intent classification + selective context pack assembly."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

from reference.agent_exec import (
    apply_model_request_opts,
    apply_stream_policy,
    build_final_answer_pack,
    detect_agent_phase,
    exclude_stale_refs,
    is_exec_intent,
    postprocess_agent_response,
    system_for_intent,
)
from context_cache import (
    ContextIndex,
    build_context_index,
    est_tokens,
    extract_last_user_query,
    save_raw_payload,
)
from adapters.memory import ingest_request, SessionState, RequestDelta, Artifact
from prompt_builder import build_memory_proxy_body, inject_memory_context, should_use_memory_body
from capture import _content_text
from chat_fast import build_simple_chat_body, strip_agent_fields

LOG = logging.getLogger("router.intent")

TOKEN_THRESHOLD = int(os.getenv("TOKEN_THRESHOLD", "20000"))
INTENT_BUDGET = int(os.getenv("INTENT_BUDGET_TOKENS", "8000"))
DEFAULT_PACK_BUDGET = int(os.getenv("CONTEXT_PACK_BUDGET", "12000"))
FETCH_LOG_LINES = int(os.getenv("INTENT_FETCH_LOG_LINES", "40"))
RECENT_AGENT_MSG_KEEP = int(os.getenv("RECENT_AGENT_MSG_KEEP", "8"))
RECENT_AGENT_MSG_CHARS = int(os.getenv("RECENT_AGENT_MSG_CHARS", "8000"))
_ROUTER_MAIN_BACKEND_RAW = os.getenv("ROUTER_MAIN_BACKEND", "fast").strip().lower()
ROUTER_MAIN_BACKEND = _ROUTER_MAIN_BACKEND_RAW if _ROUTER_MAIN_BACKEND_RAW in ("fast", "long") else "fast"
FAST_ONLY_INTENTS = frozenset({"casual", "explain"})
AGENT_FAST_FORBIDDEN = frozenset(
    {"shell_task", "log_analysis", "benchmark", "code_edit", "debug"}
)
EXEC_CONTEXT_INTENTS = frozenset({"code_edit", "benchmark", "shell_task", "log_analysis", "agent"})
EXPLAIN_PATH_INTENTS = frozenset({"explain", "project_inspection", "read_only_analysis"})
READ_ONLY_KW = (
    "코드 수정 말고",
    "수정하지 말",
    "수정하지말",
    "읽어서",
    "파일만",
    "구조 분석",
    "프로젝트 구조",
    "역할을 요약",
    "역할 요약",
    "근거와 함께",
    "read only",
    "without modifying",
)

ANALYSIS_KW = (
    "분석",
    "문제점",
    "요약",
    "검증 결과",
    "어떤 구조",
    "어떻게 처리",
    "상세분석",
    "맞는지",
    "설명해",
    "구조 파악",
    "로그 분석",
)
CODE_EDIT_STRONG_KW = (
    "수정해",
    "패치해",
    "구현해",
    "파일 바꿔",
    "코드 작성",
    "apply",
    "리팩토",
    "커밋",
    "pr ",
    "pull request",
)
CONTINUE_KW = (
    "cut off",
    "exceeded the output token",
    "continue from where you left off",
    "이어서",
    "잘렸",
)

SYSTEM_AGENT = (
    "You are a coding assistant in Cursor (local LLM).\n"
    "- Follow the latest user query as the primary task.\n"
    "- Use tools (Shell, Read, Grep) for real work; do not narrate fake plans.\n"
    "- Read files before editing; apply minimal focused changes.\n"
    "- Do not repeat prior assistant answers verbatim; execute the task.\n"
    "- Respond in Korean when user rules require it."
)


@dataclass
class IntentResult:
    intent: str
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "route": self.route,
            "needs_tools": self.needs_tools,
            "needs_files": self.needs_files,
            "needs_shell": self.needs_shell,
            "needs_prior_summary": self.needs_prior_summary,
            "needs_raw_tool_results": self.needs_raw_tool_results,
            "needs_full_raw_context": self.needs_full_raw_context,
            "context_budget_tokens": self.context_budget_tokens,
            "context_pack": self.context_pack,
            "reason": self.reason,
        }


@dataclass
class TwoPassStats:
    req_id: str = ""
    raw_tokens: int = 0
    intent_tokens: int = 0
    pack_tokens: int = 0
    saved_pct: float = 0.0
    intent: str = ""
    route: str = ""
    backend: str = ""
    route_reason: str = ""
    tools_stripped: bool = False
    needs_shell: bool = False
    mem_state: SessionState | None = None


def _match_any(text: str, patterns: list[str]) -> bool:
    lower = text.lower()
    return any(p in lower for p in patterns)


def _score_keywords(text: str, patterns: list[str], weight: float = 2.0) -> float:
    lower = text.lower()
    score = 0.0
    for pattern in patterns:
        if pattern in lower or pattern in text:
            score += weight
    return score


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


def _intent_from_name(name: str, query: str, index: ContextIndex, reason: str) -> IntentResult:
    if name == "benchmark":
        return IntentResult(
            intent="benchmark",
            route="main",
            needs_tools=True,
            needs_files=True,
            needs_shell=True,
            needs_prior_summary=True,
            needs_raw_tool_results=False,
            needs_full_raw_context=False,
            context_budget_tokens=DEFAULT_PACK_BUDGET,
            context_pack=[
                "current_query",
                "recent_files",
                "benchmark_script_refs",
                "recent_router_logs",
                "rules",
            ],
            reason=reason,
        )
    if name == "log_analysis":
        return IntentResult(
            intent="log_analysis",
            route="main",
            needs_tools=True,
            needs_files=False,
            needs_shell=True,
            needs_prior_summary=False,
            needs_raw_tool_results=True,
            needs_full_raw_context=False,
            context_budget_tokens=DEFAULT_PACK_BUDGET,
            context_pack=["current_query", "recent_router_logs", "rules"],
            reason=reason,
        )
    if name == "shell_task":
        return IntentResult(
            intent="shell_task",
            route="main",
            needs_tools=True,
            needs_files=True,
            needs_shell=True,
            needs_prior_summary=False,
            needs_raw_tool_results=False,
            needs_full_raw_context=False,
            context_budget_tokens=DEFAULT_PACK_BUDGET,
            context_pack=["current_query", "recent_files", "rules"],
            reason=reason,
        )
    if name == "debug":
        return IntentResult(
            intent="debug",
            route="main",
            needs_tools=False,
            needs_files=False,
            needs_shell=False,
            needs_prior_summary=False,
            needs_raw_tool_results=False,
            needs_full_raw_context=False,
            context_budget_tokens=6000,
            context_pack=["current_query", "project_state_summary", "rules"],
            reason=reason,
        )
    if name == "code_edit":
        return IntentResult(
            intent="code_edit",
            route="main",
            needs_tools=True,
            needs_files=True,
            needs_shell=True,
            needs_prior_summary=True,
            needs_raw_tool_results=False,
            needs_full_raw_context=False,
            context_budget_tokens=DEFAULT_PACK_BUDGET,
            context_pack=["current_query", "project_state_summary", "recent_files", "rules"],
            reason=reason,
        )
    if name == "continue_previous":
        return IntentResult(
            intent="continue_previous",
            route="main",
            needs_tools=True,
            needs_files=True,
            needs_shell=False,
            needs_prior_summary=True,
            needs_raw_tool_results=False,
            needs_full_raw_context=False,
            context_budget_tokens=DEFAULT_PACK_BUDGET,
            context_pack=["current_query", "project_state_summary", "recent_files", "rules"],
            reason=reason,
        )
    if name == "explain":
        return IntentResult(
            intent="explain",
            route="main",
            needs_tools=False,
            needs_files=False,
            needs_shell=False,
            needs_prior_summary=True,
            needs_raw_tool_results=False,
            needs_full_raw_context=False,
            context_budget_tokens=8000,
            context_pack=["current_query", "project_state_summary", "rules"],
            reason=reason,
        )
    if name == "project_inspection":
        return IntentResult(
            intent="project_inspection",
            route="main",
            needs_tools=False,
            needs_files=False,
            needs_shell=False,
            needs_prior_summary=True,
            needs_raw_tool_results=False,
            needs_full_raw_context=False,
            context_budget_tokens=8000,
            context_pack=["current_query", "project_state_summary", "rules"],
            reason=reason,
        )
    if name == "read_only_analysis":
        return IntentResult(
            intent="read_only_analysis",
            route="main",
            needs_tools=True,
            needs_files=True,
            needs_shell=False,
            needs_prior_summary=True,
            needs_raw_tool_results=False,
            needs_full_raw_context=False,
            context_budget_tokens=8000,
            context_pack=["current_query", "project_state_summary", "recent_files", "rules"],
            reason=reason,
        )
    return IntentResult(
        intent="agent",
        route="main",
        needs_tools=True,
        needs_files=True,
        needs_shell=False,
        needs_prior_summary=True,
        needs_raw_tool_results=False,
        needs_full_raw_context=False,
        context_budget_tokens=DEFAULT_PACK_BUDGET,
        context_pack=["current_query", "project_state_summary", "recent_files", "tool_result_refs", "rules"],
        reason=reason,
    )


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
    if hits >= 3:
        return True
    return False


def _is_analysis_query(q: str) -> bool:
    ql = q.lower()
    return any(kw in q for kw in ANALYSIS_KW) or any(kw in ql for kw in ("analyze", "analysis"))


def _has_strong_code_edit_signal(q: str) -> bool:
    ql = q.lower()
    return any(kw in q or kw in ql for kw in CODE_EDIT_STRONG_KW)


def classify_intent(query: str, index: ContextIndex) -> IntentResult:
    from runtime_kernel.intent import resolve_runtime_intent

    return resolve_runtime_intent(query).to_intent_result()


def _classify_intent_legacy(query: str, index: ContextIndex) -> IntentResult:
    """Deprecated — kept for regression diff only."""
    q = query.strip()
    is_analysis = _is_analysis_query(q)
    has_strong_edit = _has_strong_code_edit_signal(q)

    scores: dict[str, float] = {
        "benchmark": _score_keywords(q, ["벤치", "benchmark", "성능 측정", "tok/s", "속도 측정", "벤치마킹"]),
        "log_analysis": _score_keywords(q, ["docker logs", "서버로그", "server log"])
        + _score_keywords(q, ["로그", "log"], 1.0),
        "shell_task": _score_keywords(q, ["실행", "bash", "docker compose", "shell", "grep"]),
        "debug": _score_keywords(q, ["복붙", "다시 뱉", "반복", "같은 질문", "이전 답"]),
        "code_edit": _score_keywords(q, ["수정", "구현", "패치", "fix", "implement", "refactor", "코드", "파일"]),
        "continue_previous": _score_keywords(q, list(CONTINUE_KW) + ["이어", "계속", "continue", "previous task", "남은 작업"]),
        "explain": _score_keywords(q, ["설명", "어떻게", "왜", "what is", "explain", "알려줘"]),
        "project_inspection": 0.0,
        "read_only_analysis": 0.0,
    }

    if _is_read_only_analysis(q):
        scores["read_only_analysis"] += 10.0
        scores["explain"] += 2.0
        scores["code_edit"] = max(0.0, scores["code_edit"] - 4.0)

    if is_analysis:
        scores["explain"] += 3.0
        if "로그" in q or "log" in q.lower():
            scores["log_analysis"] += 3.0
        try:
            from reference.evidence_extractors import looks_like_project_inspection

            if looks_like_project_inspection(q):
                if _is_read_only_analysis(q):
                    scores["read_only_analysis"] += 2.0
                else:
                    scores["project_inspection"] += 4.0
        except ImportError:
            pass

    if has_strong_edit:
        scores["code_edit"] += 5.0

    # 파일 경로 — 분석 질문이면 code_edit 부스트 억제
    file_ref_count = _count_file_refs(q)
    if file_ref_count >= 3 and not is_analysis:
        scores["code_edit"] += 6.0
    elif file_ref_count >= 1 and not is_analysis:
        scores["code_edit"] += 3.0
    elif file_ref_count >= 1 and is_analysis:
        scores["project_inspection"] += 1.5

    # 스크립트 작성/실행은 shell_task
    if _match_any(q, ["스크립트", "script"]) and _match_any(
        q, ["짜", "작성", "만들", "write", "create", "벤치"]
    ):
        scores["shell_task"] += 3.0
    if _match_any(q, ["스크립트", "script"]) and _match_any(q, ["확인", "분석"]) and not has_strong_edit:
        scores["shell_task"] += 1.5

    # docker ps / docker logs / 진단 쿼리 → shell_task 가중치
    has_docker_exec = any(w in q.lower() for w in ["docker ps", "docker logs", "container", "docker exec"])
    has_diagnose = any(w in q for w in ["진단", "동작하는지", "정상", "status", "확인해", "호출"])
    file_read_task = file_ref_count >= 1 and any(w in q for w in ["읽", "read", "확인", "검증"])
    if has_docker_exec or (has_diagnose and "docker" in q.lower() and not file_read_task):
        scores["shell_task"] += 4.0

    # curl/테스트는 shell 실행이 필요하지만 단독으론 intent 결정에 미포함
    needs_shell_hint = "curl" in q.lower() or "테스트" in q or "실행" in q or has_docker_exec

    # 로그+분석/개선 → log_analysis / explain (code_edit 아님)
    if ("로그" in q or "log" in q.lower()) and any(w in q for w in ["개선", "분석", "기반", "라우터", "문제점"]):
        if not has_docker_exec and not has_strong_edit:
            scores["log_analysis"] += 2.5
            scores["explain"] += 1.5
            scores["code_edit"] = max(0.0, scores["code_edit"] - 1.0)

    if any(w in q for w in ["읽고", "확인해", "검증", "정리"]) and not has_docker_exec:
        if has_strong_edit and not is_analysis:
            scores["code_edit"] += 1.5
        elif is_analysis:
            scores["explain"] += 1.0

    best = max(scores, key=lambda name: scores[name])
    best_score = scores[best]
    # Log full score breakdown for analysis
    score_parts = "  ".join(
        f"{'→' if n == best else ' '}{n}={v:.1f}" for n, v in sorted(scores.items(), key=lambda x: -x[1])
    )
    LOG.info(
        "intent_scores files=%d docker_exec=%s diagnose=%s | %s",
        file_ref_count,
        str(has_docker_exec).lower(),
        str(has_diagnose).lower(),
        score_parts,
    )
    if best_score >= 2.0:
        result = _intent_from_name(best, q, index, reason=f"score={best_score:.1f} files={file_ref_count}")
        # curl/테스트/실행이 포함된 요청이면 needs_shell=true 부착 (intent는 유지)
        if needs_shell_hint and not result.needs_shell:
            result.needs_shell = True
        return result

    if len(q) <= 200 and not _match_any(
        q,
        [
            "스크립트",
            "script",
            "서버",
            "분석",
            "확인",
            "짜",
            "작성",
            "수정",
            "구현",
            "파일",
            "코드",
            "docker",
            "grep",
            "benchmark",
            "벤치",
        ],
    ):
        return IntentResult(
            intent="casual",
            route="fast",
            needs_tools=False,
            needs_files=False,
            needs_shell=False,
            needs_prior_summary=False,
            needs_raw_tool_results=False,
            needs_full_raw_context=False,
            context_budget_tokens=2000,
            context_pack=["current_query"],
            reason="short casual question",
        )

    return _intent_from_name("agent", q, index, reason="default agent task")


def _fetch_docker_logs(container: str, lines: int = FETCH_LOG_LINES) -> str:
    try:
        r = subprocess.run(
            ["docker", "logs", container, "--tail", str(lines)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        text = ((r.stdout or "") + (r.stderr or "")).strip()
        return text[-4000:] if len(text) > 4000 else text
    except Exception as exc:
        return f"(log fetch failed: {exc})"


def _benchmark_script_refs(index: ContextIndex) -> str:
    refs = [f for f in index.recent_files + index.file_refs if "benchmark" in f.lower() or "scripts/" in f]
    refs.extend(
        [
            "scripts/benchmark-router-live.py",
            "scripts/analyze-conversation-flow.py",
            "scripts/test-router.sh",
        ]
    )
    uniq = list(dict.fromkeys(refs))[:10]
    return "\n".join(f"- {p}" for p in uniq)


def build_context_pack(intent: IntentResult, index: ContextIndex, body: dict[str, Any]) -> str:
    pack_sections, _ = exclude_stale_refs(intent.intent, index.query, intent.context_pack)
    parts: list[str] = []
    budget = intent.context_budget_tokens

    if "current_query" in pack_sections:
        parts.extend(["[Task]", index.query or extract_last_user_query(body)])

    if "project_state_summary" in pack_sections and index.project_summary and not is_exec_intent(intent.intent):
        parts.extend(["", "[Relevant project state]", index.project_summary[:1200]])

    if "recent_files" in pack_sections and index.recent_files:
        parts.extend(["", "[Relevant files]", "\n".join(f"- {f}" for f in index.recent_files[:10])])

    if "benchmark_script_refs" in pack_sections:
        parts.extend(["", "[Benchmark/script refs]", _benchmark_script_refs(index)])

    if "recent_router_logs" in pack_sections:
        router_logs = _fetch_docker_logs("cursor-local-llm-router", FETCH_LOG_LINES)
        llama_logs = _fetch_docker_logs("cursor-local-llm-fast", min(20, FETCH_LOG_LINES))
        parts.extend(
            [
                "",
                "[Cached logs]",
                "### router",
                router_logs or "(empty)",
                "",
                "### llama-fast",
                llama_logs or "(empty)",
            ]
        )

    if "tool_result_refs" in pack_sections and index.tool_results:
        lines = []
        for tr in index.tool_results[-8:]:
            lines.append(f"- {tr.name} hash={tr.hash[:8]} chars={tr.chars} preview={tr.preview[:100]}")
        parts.extend(["", "[Tool result refs]", "\n".join(lines)])

    if "rules" in pack_sections and index.rules_summary:
        parts.extend(["", index.rules_summary[:800]])

    pack = "\n".join(parts).strip()
    if est_tokens(pack) > budget:
        pack = pack[: budget * 3]
    return pack


FINAL_ANSWER_FAST_THRESHOLD = int(os.getenv("FINAL_ANSWER_FAST_THRESHOLD", "6000"))
from runtime_kernel.constants import TOOL_PLANNING_MAX_TOKENS


def _log_proxy_body_summary(proxy_body: dict[str, Any], phase: str) -> None:
    """Log a concise summary of what is being sent to the LLM for debugging."""
    msgs = proxy_body.get("messages", [])
    role_seq = [m.get("role", "?") for m in msgs if isinstance(m, dict)]
    tools = proxy_body.get("tools", [])
    tool_names = [t.get("function", {}).get("name", "?") for t in tools]
    max_tok = proxy_body.get("max_tokens", "?")
    # Summarize each message: role + content length + has_tool_calls
    msg_parts = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "?")
        content = m.get("content", "")
        chars = len(content) if isinstance(content, str) else len(str(content))
        tc = len(m.get("tool_calls") or [])
        msg_parts.append(f"{role}[{chars}{'T' if tc else ''}]")
    LOG.info(
        "proxy_body phase=%s msgs=%d tools=%d max_tokens=%s | %s | tools=[%s]",
        phase,
        len(msgs),
        len(tools),
        max_tok,
        " → ".join(msg_parts),
        ",".join(tool_names[:8]) + ("…" if len(tool_names) > 8 else ""),
    )


def route_backend(
    intent: IntentResult,
    pack_tokens: int,
    sticky_long: bool,
    agent_phase: str = "",
    *,
    active_backend: str = "",
) -> tuple[str, str]:
    """Select llama backend. intent.route is logical; backend is physical fast|long."""
    _ = active_backend  # reserved for future soft-switch; compressed packs never keep warm long
    if intent.needs_full_raw_context or pack_tokens > TOKEN_THRESHOLD:
        return "long", "pack_exceeds_threshold"
    if intent.route == "long":
        return "long", "intent_route_long"
    phase = agent_phase or ""
    if phase == "tool_planning":
        if intent.intent in ("read_only_analysis", "project_inspection"):
            return "fast", "tool_planning_read_only_fast"
        if intent.intent not in AGENT_FAST_FORBIDDEN and not intent.needs_full_raw_context:
            return "fast", "tool_planning_small_pack"
        return "fast", "tool_planning_compressed_fast"
    if intent.intent in AGENT_FAST_FORBIDDEN:
        return "fast", "compressed_pack_fast"
    if intent.intent in FAST_ONLY_INTENTS and intent.route == "fast":
        return "fast", "casual_or_explain"
    return "fast", "compressed_pack_fast"


def default_proxy_phase(intent: IntentResult, detected: str) -> str:
    if detected:
        return detected
    if intent.intent in ("explain", "casual", "project_inspection", "read_only_analysis"):
        return ""
    if intent.needs_tools:
        return "tool_planning"
    return ""


def _apply_tools_policy(
    out: dict[str, Any],
    body: dict[str, Any],
    intent: IntentResult,
    query: str,
    phase: str,
    state: SessionState | None,
) -> bool:
    """Return tools_stripped flag after keep/strip/inject policy."""
    from reference.planner import AgentPlan, ensure_proxy_tools, filter_tools_by_plan, should_keep_tools, should_strip_tools

    plan = AgentPlan.from_dict(state.agent_plan) if state and state.agent_plan else None
    cur_phase = phase or "tool_planning"
    if should_strip_tools(plan, intent.intent, query, body, cur_phase):
        strip_agent_fields(out)
        return True
    # Exec intents: inject source/path tools even when Cursor sends tools=[] (common on compressed packs).
    if intent.needs_tools and cur_phase == "tool_planning":
        ensure_proxy_tools(out, body, plan)
        if plan and out.get("tools"):
            filter_tools_by_plan(out, plan)
        return not bool(out.get("tools"))
    if should_keep_tools(plan, intent.intent, query, body, cur_phase):
        ensure_proxy_tools(out, body, plan)
        if plan and out.get("tools"):
            filter_tools_by_plan(out, plan)
        return not bool(out.get("tools"))
    if not intent.needs_tools:
        strip_agent_fields(out)
        return True
    return not bool(out.get("tools"))


def build_proxy_body(
    body: dict[str, Any],
    intent: IntentResult,
    index: ContextIndex,
    *,
    state: SessionState | None = None,
    delta: RequestDelta | None = None,
    artifacts: list[Artifact] | None = None,
    backend: str = "long",
) -> tuple[dict[str, Any], bool, bool, bool, str]:
    """Return (proxy_body, tools_stripped, stream_forced, stale_refs_excluded, phase)."""
    stale_refs_excluded = False
    pack_sections, stale_refs_excluded = exclude_stale_refs(intent.intent, index.query, intent.context_pack)
    intent.context_pack = pack_sections

    if state and index.query:
        try:
            from reference.planner import ensure_agent_plan

            msgs = body.get("messages")
            ensure_agent_plan(
                state,
                index.query,
                router_intent=intent.intent,
            )
        except Exception:
            pass

    phase = default_proxy_phase(
        intent,
        detect_agent_phase(
            body,
            intent.intent,
            intent.needs_tools,
            state=state,
            query=index.query,
        )
        or "",
    )

    if should_use_memory_body(intent.intent, state or SessionState()) and state and delta is not None:
        mem_phase = phase or (
            "tool_planning"
            if is_exec_intent(intent.intent) or intent.intent in ("agent", "debug", "shell_task")
            else ""
        )
        out, mem_phase = build_memory_proxy_body(
            body,
            state,
            delta,
            artifacts or [],
            intent.intent,
            mem_phase,
            backend,
            index,
            query=index.query,
        )
        phase = mem_phase or phase
        stream_forced = apply_stream_policy(out, intent.intent, intent.needs_tools, intent.needs_shell)
        tools_stripped = _apply_tools_policy(
            out, body, intent, index.query, phase or mem_phase, state
        )
        _log_proxy_body_summary(out, phase or intent.intent)
        return out, tools_stripped, stream_forced, stale_refs_excluded, phase

    if intent.route == "fast" and intent.intent == "casual":
        out = build_simple_chat_body(body)
        strip_agent_fields(out)
        return out, True, False, stale_refs_excluded, ""

    messages = body.get("messages", [])
    last_role = messages[-1].get("role") if isinstance(messages, list) and messages else ""

    if phase in ("final_answer", "partial_final_answer", "recovery_final") and last_role == "tool":
        # Build final answer context using the actual session messages (tool calls + results)
        # since the last user message, rather than a compressed pack.
        # This ensures the model can reference real tool output without hallucinating.
        last_user_idx = -1
        for i, m in enumerate(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user_idx = i
        original_user_msg = (
            messages[last_user_idx]
            if last_user_idx >= 0
            else {"role": "user", "content": index.query or extract_last_user_query(body)}
        )
        # Include tool calls + results from the current agent session only
        session_tail = messages[last_user_idx + 1 :] if last_user_idx >= 0 else []
        out = copy.deepcopy(body)
        out["messages"] = [
            {"role": "system", "content": system_for_intent(intent.intent, phase=phase or "final_answer")},
            copy.deepcopy(original_user_msg),
            *[copy.deepcopy(m) for m in session_tail],
        ]
        # No tools in final_answer — model must write prose only
        strip_agent_fields(out)
        stream_forced = apply_stream_policy(out, intent.intent, False, False)
        _log_proxy_body_summary(out, phase)
        return out, True, stream_forced, stale_refs_excluded, phase

    pack = build_context_pack(intent, index, body)
    out = copy.deepcopy(body)
    proxy_messages: list[dict[str, Any]] = []

    if intent.intent in EXEC_CONTEXT_INTENTS:
        original_system = extract_original_system(body)
        if original_system:
            # Append shell execution hint when task requires it — do not replace Cursor's system
            if intent.needs_shell:
                orig_content = _content_text(original_system.get("content", ""))
                shell_hint = (
                    "\n\n[Execution requirement] This task requires real shell execution. "
                    "You MUST call the Shell tool for any 'curl', 'run', 'docker', '실행', '테스트', '검증', '확인', '로그', 'ps', 'logs' steps. "
                    "Do NOT invent or assume command outputs — call Shell and use the real result."
                )
                original_system = {"role": "system", "content": orig_content + shell_hint}
            proxy_messages.append(original_system)
        else:
            # No Cursor-provided system → inject tool_planning instructions as fallback
            proxy_messages.append({
                "role": "system",
                "content": system_for_intent(intent.intent, phase="tool_planning"),
            })
        proxy_messages.extend(extract_recent_agent_tail(body))
        proxy_messages.append({"role": "user", "content": pack})
        out["messages"] = proxy_messages
    else:
        out["messages"] = [
            {"role": "system", "content": system_for_intent(intent.intent, phase="tool_planning")},
            {"role": "user", "content": pack},
        ]

    stream_forced = apply_stream_policy(out, intent.intent, intent.needs_tools, intent.needs_shell)
    tools_stripped = _apply_tools_policy(
        out, body, intent, index.query, phase or "tool_planning", state
    )
    if tools_stripped:
        return out, True, stream_forced, stale_refs_excluded, phase or "tool_planning"

    # Clamp max_tokens for tool_planning: model only needs to emit tool_call JSON
    cur_phase = phase or "tool_planning"
    if cur_phase == "tool_planning":
        existing = out.get("max_tokens")
        if not isinstance(existing, int) or existing > TOOL_PLANNING_MAX_TOKENS:
            out["max_tokens"] = TOOL_PLANNING_MAX_TOKENS

    _log_proxy_body_summary(out, cur_phase)
    return out, False, stream_forced, stale_refs_excluded, cur_phase


def process_two_pass(
    body: dict[str, Any], sticky_long: bool = False, *, active_backend: str = ""
) -> tuple[dict[str, Any], str, TwoPassStats, IntentResult, str]:
    stats = TwoPassStats()
    stats.raw_tokens = max(1, len(json.dumps(body, ensure_ascii=False)) // 3)

    req_id = save_raw_payload(body)
    stats.req_id = req_id
    query = extract_last_user_query(body)
    try:
        from adapters.observe import begin_run, set_current_run_id

        begin_run(req_id, query=query, flow_id=req_id)
        set_current_run_id(req_id)
    except Exception:
        pass
    _delta, _mem_state, _artifacts = ingest_request(req_id, body, query=query)
    _mem_state.last_raw_tokens = int(stats.raw_tokens)
    stats.mem_state = _mem_state
    index = build_context_index(body, req_id, state=_mem_state, delta=_delta)

    intent_prompt = index.to_intent_prompt()
    stats.intent_tokens = est_tokens(intent_prompt)

    intent = classify_intent(index.query, index)
    stats.intent = intent.intent
    stats.route = intent.route

    from runtime_kernel.intent import resolve_runtime_intent
    from runtime_kernel.runtime_state import build_runtime_state, persist_runtime_state
    from runtime_kernel.self_model import format_self_model_block

    _intent_resolution = resolve_runtime_intent(index.query)
    try:
        from adapters.observe import update_run_meta

        update_run_meta(req_id, intent=intent.intent)
    except Exception:
        pass

    proxy_body, tools_stripped, stream_forced, stale_refs_excluded, agent_phase = build_proxy_body(
        body,
        intent,
        index,
        state=_mem_state,
        delta=_delta,
        artifacts=_artifacts,
        backend=ROUTER_MAIN_BACKEND,
    )
    _runtime_state = build_runtime_state(
        turn_id=req_id,
        flow_id=req_id,
        query=index.query,
        phase=agent_phase or "tool_planning",
        intent=_intent_resolution,
        session_state=_mem_state,
        self_model_excerpt=format_self_model_block(max_chars=1200),
    )
    persist_runtime_state(_mem_state, _runtime_state)
    proxy_body = inject_memory_context(proxy_body, _mem_state, _delta, _artifacts)
    stats.tools_stripped = tools_stripped
    stats.needs_shell = intent.needs_shell
    stats.pack_tokens = est_tokens(json.dumps(proxy_body.get("messages", []), ensure_ascii=False))
    if stats.raw_tokens > 0:
        stats.saved_pct = round(100 * (1 - stats.pack_tokens / stats.raw_tokens), 1)

    try:
        from adapters.memory import extract_workspace_path, project_key_from_workspace, save_state

        ws = extract_workspace_path(body)
        pk = project_key_from_workspace(ws)
        save_state(_mem_state, pk if pk != "unknown" else None)
    except Exception:
        pass

    backend, route_reason = route_backend(
        intent,
        stats.pack_tokens,
        sticky_long,
        agent_phase=agent_phase,
        active_backend=active_backend,
    )
    stats.backend = backend
    stats.route_reason = route_reason

    LOG.info(
        "pass=intent req_id=%s query_tokens=%d index_tokens=%d intent=%s route=%s reason=%s",
        req_id,
        est_tokens(index.query),
        stats.intent_tokens,
        intent.intent,
        intent.route,
        intent.reason,
    )
    LOG.info(
        "pass=context_pack req_id=%s raw_tokens=%d pack_tokens=%d saved=%.1f%% tools_stripped=%s needs_shell=%s",
        req_id,
        stats.raw_tokens,
        stats.pack_tokens,
        stats.saved_pct,
        str(tools_stripped).lower(),
        str(intent.needs_shell).lower(),
    )
    LOG.info(
        "route intent=%s intent_route=%s backend=%s reason=%s tools_stripped=%s context_pack=%s",
        intent.intent,
        intent.route,
        backend,
        route_reason,
        str(tools_stripped).lower(),
        ",".join(intent.context_pack),
    )
    if is_exec_intent(intent.intent) or agent_phase:
        LOG.info(
            "agent_exec intent=%s phase=%s stream_forced=%s stale_refs_excluded=%s",
            intent.intent,
            agent_phase or "none",
            str(stream_forced).lower(),
            str(stale_refs_excluded).lower(),
        )

    apply_model_request_opts(
        proxy_body,
        intent.intent,
        agent_phase or "",
        plan_phase=_mem_state.phase_hint if _mem_state else "",
        query=index.query,
    )

    from runtime_core.prompt_enforcer import enforce_prompt_budget

    proxy_body, _shrunk = enforce_prompt_budget(proxy_body, backend, agent_phase or "")
    stats.pack_tokens = est_tokens(json.dumps(proxy_body.get("messages", []), ensure_ascii=False))
    if stats.raw_tokens > 0:
        stats.saved_pct = round(100 * (1 - stats.pack_tokens / stats.raw_tokens), 1)

    return proxy_body, backend, stats, intent, agent_phase
