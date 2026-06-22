"""Agent execution guards: stream policy, stale-ref exclusion, tool-call recovery."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from capture import _content_text

LOG = logging.getLogger("router.agent_exec")

AgentPhase = Literal["tool_planning", "final_answer", "partial_final_answer", "recovery_final"]

from runtime_kernel.constants import EXEC_INTENTS
NO_THINK_INTENTS = frozenset(
    {"explain", "casual", "agent", "debug", "shell_task", "code_edit", "benchmark", "log_analysis"}
)
EXPLAIN_MIN_MAX_TOKENS = int(os.getenv("EXPLAIN_MIN_MAX_TOKENS", "256"))

READ_ONLY_INTENTS = frozenset({"read_only_analysis", "project_inspection"})

RUNTIME_PHASE_OVERRIDE = (
    "[Runtime Phase Override]\n"
    "The following phase instruction has higher priority than preserved Cursor system instructions."
)

RUNTIME_PRIORITY_ORDER = (
    "Priority order:\n"
    "1. Runtime Phase Instruction\n"
    "2. Current Task\n"
    "3. Safety / Hard Guards\n"
    "4. PlannerDecision or AgentPlan\n"
    "5. Evidence / Tool Results\n"
    "6. Session / Journal / Handoff\n"
    "7. Preserved Cursor rules"
)

RUNTIME_REASONING_POLICY = (
    "- Model native reasoning (Qwen thinking) is recorded in explorer trace for live debugging only; "
    "do not repeat it verbatim in user-facing answers.\n"
    "- Decision trace stores PlannerDecision summary (action, reason, confidence) — not full chain-of-thought."
)

FINAL_KOREAN_POLICY = (
    "- Final user-facing answers must be written in Korean unless the user explicitly asks otherwise.\n"
    "- Internal planning/tool calls may use English field names, but user-facing prose must be Korean."
)

TOOL_PLANNING_KOREAN_POLICY = (
    "- Do not emit user-facing prose. "
    "Tool call arguments may contain Korean search/query text when the user query is Korean."
)


def allowed_tools_line(intent_name: str) -> str:
    if intent_name in READ_ONLY_INTENTS:
        return (
            "- For read_only_analysis, Shell and Edit are forbidden. Use only Read, Grep, Glob.\n"
            "- Only emit tool_calls using allowed tools for this phase: Read, Grep, Glob."
        )
    return "- Only emit tool_calls using allowed tools for this phase: Read, Grep, Glob, Shell."


def _build_tool_planning_body(intent_name: str, *, retry: bool = False) -> str:
    lines = [
        "You are in TOOL PLANNING phase (Cursor local LLM).",
        "- Output NO user-facing prose. Do not explain what you are about to do.",
        allowed_tools_line(intent_name),
        TOOL_PLANNING_KOREAN_POLICY,
        "- Do not write shell commands in markdown code fences.",
        "- Do not print context pack labels like [Task] or [Relevant project state].",
        "- Do not say \"I'll help you\" or similar filler.",
    ]
    if intent_name not in READ_ONLY_INTENTS:
        lines.extend([
            "- If the task says 'test', 'curl', 'run', 'docker', '테스트', '실행', '검증', '확인', "
            "'로그', 'ps', 'logs', 'status': you MUST emit a Shell tool_call.",
            "- Read files first, then verify with Shell when results or live state is requested.",
        ])
    lines.append("- If you cannot emit tool_call, respond with exactly: TOOL_CALL_UNAVAILABLE.")
    if retry:
        lines.append("RETRY: Call Shell now. No prose. tool_calls only.")
    return "\n".join(lines)


def _build_final_answer_body() -> str:
    return "\n".join([
        "You are in FINAL ANSWER phase (Cursor local LLM).",
        FINAL_KOREAN_POLICY,
        "- Produce the final answer using the Task, collected evidence, Evidence Anchors, Task Journal, "
        "Final Report block, and RuntimeState when present.",
        "- Do not introduce unsupported facts outside the provided runtime context.",
        "- Prefer the Final Report Renderer output when present; use LLM prose only to polish gaps or "
        "add structure — do not discard renderer content.",
        "- Organize sections by topics or components the user asked about — do not assume a fixed module list.",
        "- Cite concrete paths, directory names, and excerpts from README, docs, and directory listings.",
        "- Do NOT emit tool_calls.",
        "- Do NOT print context pack labels ([Task], [Relevant project state], etc.).",
        "- Do NOT repeat \"I'll help you\" or planning phrases.",
        "- For each requested area: 2-4 sentences on responsibility and key files when evidence exists.",
        "- Forbidden: lazy one-line dismissals (e.g. '정보 없음', '미확인') when relevant evidence is present.",
        "- Summarize configuration values and test output only when the user asked for them.",
    ])


def _build_default_assistant_body() -> str:
    return "\n".join([
        "You are a coding assistant in Cursor (local LLM).",
        FINAL_KOREAN_POLICY,
        "- Follow the latest user query as the primary task.",
        "- Use tools when needed.",
    ])


def build_runtime_system(
    intent_name: str,
    *,
    phase: AgentPhase | None = None,
    retry: bool = False,
    preserved_cursor: bool = False,
) -> str:
    """Runtime header + phase instruction (no preserved Cursor content)."""
    parts: list[str] = []
    if preserved_cursor:
        parts.append(RUNTIME_PHASE_OVERRIDE)
    parts.append(RUNTIME_PRIORITY_ORDER)
    parts.append(RUNTIME_REASONING_POLICY)
    if phase in ("final_answer", "partial_final_answer", "recovery_final"):
        parts.append(_build_final_answer_body())
    elif phase == "tool_planning":
        parts.append(_build_tool_planning_body(intent_name, retry=retry))
    else:
        parts.append(_build_default_assistant_body())
    return "\n\n".join(parts)


def compose_proxy_system(
    intent_name: str,
    *,
    phase: AgentPhase | None = None,
    retry: bool = False,
    preserved_cursor_content: str = "",
    shell_hint: str = "",
) -> str:
    """Phase system with optional preserved Cursor rules at priority 7."""
    preserved = bool(preserved_cursor_content.strip())
    base = build_runtime_system(
        intent_name,
        phase=phase,
        retry=retry,
        preserved_cursor=preserved,
    )
    if not preserved:
        return base
    cursor_block = preserved_cursor_content.strip()
    if shell_hint:
        cursor_block = f"{cursor_block}\n{shell_hint.strip()}"
    return f"{base}\n\n[Preserved Cursor Rules — priority 7]\n{cursor_block}"


# Backward-compatible module-level constants (default exec intent)
SYSTEM_TOOL_PLANNING = _build_tool_planning_body("agent")
SYSTEM_FINAL_ANSWER = _build_final_answer_body()
RETRY_SYSTEM = _build_tool_planning_body("agent", retry=True)

BASH_FENCE_RE = re.compile(r"```(?:bash|sh)\s*\n(.*?)```", re.S | re.I)

PACK_LABEL_LINE_RE = re.compile(
    r"^\s*\[(?:Task|Relevant project state|prior context summary|Cached logs|"
    r"Tool result refs|Benchmark/script refs|Relevant files|고정 규칙)\]",
    re.I | re.M,
)

FILLER_LINE_RE = re.compile(
    r"^(?:I'll help you|Let me first|I will help|Sure[,!]?|Okay[,!]?|"
    r"먼저 .{0,20}확인하겠습니다\.?|확인해 ?보겠습니다\.?|도와드리겠습니다\.?)\s*$",
    re.I | re.M,
)

BASH_FENCE_RE = re.compile(r"```(?:bash|sh)\s*\n(.*?)```", re.S | re.I)

# Qwen Coder sometimes emits legacy XML tool calls instead of OpenAI tool_calls JSON.
FUNCTION_XML_RE = re.compile(r"<function=(\w+)>", re.I)
PARAMETER_XML_RE = re.compile(r"<parameter=(\w+)>\s*\n?([^<\n][^\n<]*)", re.I)
XML_TOOL_BLOCK_RE = re.compile(r"<tool_call>.*?</tool_call>|<function=\w+>.*?(?=\n\n|\Z)", re.S | re.I)

ALLOWLIST_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^pwd\s*$"),
    re.compile(r"^ls(?:\s+[-\w./~]+)?\s*$"),
    re.compile(r"^head\s+-n\s+\d+\s+[\w./~ -]+$"),
    re.compile(r"^tail\s+-n\s+\d+\s+[\w./~ -]+$"),
    re.compile(r"^cat\s+[\w./~ -]+$"),
    re.compile(r"^docker\s+(ps|logs)\b[\w./: -]*$"),
    re.compile(r"^python3\s+-c\s+.+$"),
    re.compile(r"^curl\s+-sS\s+http://localhost:8080[\w./?=&:-]*$"),
    re.compile(r"^rg\s+[\w./'\" -]+$"),
    re.compile(r"^history(?:\s+\|\s+tail\s+-n\s+\d+)?\s*$"),
]


@dataclass
class AgentExecLog:
    intent: str = ""
    phase: str = ""
    stream_forced: bool = False
    stale_refs_excluded: bool = False
    tool_calls: int = 0
    retry: int = 0
    markdown_shell_detected: bool = False
    synthetic_tool_call: bool = False
    router_executed: bool = False
    fallback: bool = False
    content_stripped: bool = False
    content_sanitized: bool = False


def is_exec_intent(intent_name: str) -> bool:
    return intent_name in EXEC_INTENTS


def wants_prior_refs(query: str) -> bool:
    return _match_any(query, ["이전 결과", "prior result", "cached result", "이전 tool", "previous tool"])


def _match_any(text: str, patterns: list[str]) -> bool:
    lower = text.lower()
    return any(p in lower for p in patterns)


def exclude_stale_refs(intent_name: str, query: str, context_pack: list[str]) -> tuple[list[str], bool]:
    """Remove stale tool refs from exec intents unless user explicitly asks."""
    if intent_name not in EXEC_INTENTS and intent_name != "debug":
        return context_pack, False
    if wants_prior_refs(query):
        return context_pack, False
    before = list(context_pack)
    excluded = [s for s in context_pack if s != "tool_result_refs"]
    # shell_task: minimal pack
    if intent_name == "shell_task":
        allowed = {"current_query", "recent_files", "rules"}
        excluded = [s for s in excluded if s in allowed]
    changed = excluded != before
    return excluded, changed


def apply_stream_policy(body: dict[str, Any], intent_name: str, needs_tools: bool, needs_shell: bool) -> bool:
    """Force stream=false for tool execution. Returns True if forced."""
    agent_like = intent_name in ("agent", "debug", "shell_task")
    if (
        intent_name in EXEC_INTENTS
        or needs_shell
        or agent_like
        or (needs_tools and intent_name not in ("casual", "explain"))
    ):
        body["stream"] = False
        return True
    return False


MIN_TOOL_CALLS_FOR_FINAL_ANSWER = int(os.getenv("MIN_TOOL_CALLS_FOR_FINAL_ANSWER", "3"))


def _count_tool_calls_after_last_user(messages: list[dict[str, Any]], last_user: int) -> int:
    """Count distinct tool_call turns (assistant messages with tool_calls) since last user."""
    count = 0
    for msg in messages[last_user + 1 :]:
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("tool_calls"):
            count += 1
    return count


def detect_agent_phase(
    body: dict[str, Any],
    intent_name: str,
    needs_tools: bool,
    state: Any | None = None,
    query: str = "",
) -> AgentPhase | None:
    from .plan_state import resolve_agent_phase

    return resolve_agent_phase(body, state, query, intent_name, needs_tools)


FINAL_ANSWER_MAX_CHARS = int(os.getenv("FINAL_ANSWER_MAX_CHARS", "24000"))
FINAL_ANSWER_PER_TOOL_CHARS = int(os.getenv("FINAL_ANSWER_PER_TOOL_CHARS", "6000"))
MIN_FINAL_CHARS = int(os.getenv("MIN_FINAL_CHARS", "200"))


def extract_recent_tool_outputs(
    body: dict[str, Any],
    max_chars: int | None = None,
    per_tool_chars: int | None = None,
    since_last_user: bool = True,
) -> str:
    """Extract tool results from the body. By default only includes results after the last user message."""
    max_chars = FINAL_ANSWER_MAX_CHARS if max_chars is None else max_chars
    per_tool_chars = FINAL_ANSWER_PER_TOOL_CHARS if per_tool_chars is None else per_tool_chars
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return ""
    if since_last_user:
        last_user = -1
        for i, m in enumerate(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user = i
        messages = messages[last_user + 1 :] if last_user >= 0 else messages
    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        name = str(msg.get("name", "tool"))
        text = _content_text(msg.get("content", "")).strip()
        if not text or (text.startswith("[tool ") and len(text) < 250):
            continue
        parts.append(f"### {name}\n{text[:per_tool_chars]}")
    out = "\n\n".join(parts[-10:])
    return out[:max_chars]


def build_final_answer_pack(query: str, body: dict[str, Any]) -> str:
    messages = body.get("messages", [])
    tool_msg_count = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "tool")
    # 파일 수가 많을수록 per_tool 한도를 늘려 잘림 방지
    per_tool = max(FINAL_ANSWER_PER_TOOL_CHARS, min(12000, FINAL_ANSWER_MAX_CHARS // max(tool_msg_count, 1)))
    tool_out = extract_recent_tool_outputs(body, per_tool_chars=per_tool)
    parts = ["[User query]", query]
    if tool_out:
        parts.extend(["", "[Tool results]", tool_out])
    else:
        parts.extend(["", "[Tool results]", "(no tool output in request yet)"])
    return "\n".join(parts)


def system_for_phase(
    phase: AgentPhase | None,
    retry: bool = False,
    *,
    intent_name: str = "agent",
    preserved_cursor: bool = False,
) -> str:
    return build_runtime_system(
        intent_name,
        phase=phase,
        retry=retry,
        preserved_cursor=preserved_cursor,
    )


def system_for_intent(
    intent_name: str,
    retry: bool = False,
    phase: AgentPhase | None = None,
    *,
    preserved_cursor: bool = False,
) -> str:
    if phase is not None:
        return system_for_phase(
            phase,
            retry=retry,
            intent_name=intent_name,
            preserved_cursor=preserved_cursor,
        )
    if intent_name in EXEC_INTENTS or retry:
        return build_runtime_system(
            intent_name,
            phase="tool_planning",
            retry=retry,
            preserved_cursor=preserved_cursor,
        )
    return system_for_phase(None, intent_name=intent_name, preserved_cursor=preserved_cursor)


def sanitize_response_content(content: str) -> tuple[str, bool]:
    if not content:
        return "", False
    original = content
    # Drop lines that are pack labels or pure filler
    lines: list[str] = []
    for line in content.splitlines():
        if PACK_LABEL_LINE_RE.match(line):
            continue
        if FILLER_LINE_RE.match(line.strip()):
            continue
        if line.strip().startswith("[prior context summary]"):
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    # Remove inline pack label blocks
    cleaned = PACK_LABEL_LINE_RE.sub("", cleaned).strip()
    if "I'll help you" in cleaned[:200]:
        cleaned = re.sub(r"I'll help you[^.\n]*\.?\s*", "", cleaned, count=1, flags=re.I).strip()
    return cleaned, cleaned != original


def is_empty_content(value: Any) -> bool:
    if value is None:
        return True
    if value == []:
        return True
    if isinstance(value, str) and value.strip().lower() in ("", "null", "none"):
        return True
    return False


def user_facing_content(value: Any) -> str:
    if is_empty_content(value):
        return ""
    if isinstance(value, list):
        return ""
    return str(value).strip()


def ensure_tool_call_ids(response: dict[str, Any]) -> bool:
    """Ensure every tool_call has an id (required by Cursor agent loop)."""
    changed = False
    try:
        msg = response["choices"][0]["message"]
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict) and not tc.get("id"):
                tc["id"] = f"call_{secrets.token_hex(8)}"
                changed = True
    except (KeyError, IndexError, TypeError):
        return False
    return changed


def _reasoning_to_answer(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else text.strip()


def apply_model_request_opts(
    body: dict[str, Any],
    intent_name: str = "",
    phase: str = "",
    plan_phase: str = "",
    query: str = "",
) -> None:
    """Per-request llama-server options: Qwen thinking + coding sampling."""
    from qwen_request import apply_qwen_request_opts

    apply_qwen_request_opts(body, intent_name, phase, plan_phase)

    if intent_name in ("explain", "casual"):
        mt = body.get("max_tokens")
        if not isinstance(mt, int) or mt < EXPLAIN_MIN_MAX_TOKENS:
            body["max_tokens"] = EXPLAIN_MIN_MAX_TOKENS
    elif phase == "final_answer":
        from .answer_tokens import apply_final_answer_tokens

        apply_final_answer_tokens(body, intent_name, query)


def normalize_client_response(response: dict[str, Any], phase: AgentPhase | None = None) -> dict[str, Any]:
    """Ensure client-visible responses never carry null/None as content."""
    out = copy.deepcopy(response)
    try:
        msg = out["choices"][0]["message"]
        choice = out["choices"][0]
    except (KeyError, IndexError, TypeError):
        return out

    if msg.get("tool_calls"):
        ensure_tool_call_ids(out)
        msg = out["choices"][0]["message"]
        msg["content"] = ""
        choice["finish_reason"] = "tool_calls"
    elif phase == "tool_planning":
        if msg.get("tool_calls"):
            msg["content"] = ""
        else:
            msg["content"] = user_facing_content(msg.get("content"))
    elif phase in ("final_answer", "partial_final_answer", "recovery_final"):
        msg["content"] = user_facing_content(msg.get("content"))
        if is_empty_content(msg.get("content")):
            reasoning = str(msg.get("reasoning_content") or "").strip()
            if reasoning:
                msg["content"] = _reasoning_to_answer(reasoning)
    elif is_empty_content(msg.get("content")):
        msg["content"] = ""

    # Keep reasoning_content for Cursor thinking UI (router-injected or model).
    return out


def finalize_client_response(
    response: dict[str, Any],
    intent_name: str = "",
    phase: str = "",
) -> dict[str, Any]:
    phase_norm: AgentPhase | None = None
    if phase in ("tool_planning", "final_answer", "partial_final_answer", "recovery_final"):
        phase_norm = phase  # type: ignore[assignment]
    return normalize_client_response(response, phase_norm)


def strip_tool_call_content(response: dict[str, Any]) -> bool:
    if not has_tool_calls(response):
        return False
    msg = response["choices"][0]["message"]
    msg["content"] = ""
    response["choices"][0]["finish_reason"] = "tool_calls"
    return True


def _sse_chunk(base: dict[str, Any], delta: dict[str, Any], finish: str | None) -> str | None:
    if "content" in delta and is_empty_content(delta.get("content")):
        delta = {k: v for k, v in delta.items() if k != "content"}
    if not delta and finish is None:
        return None
    obj = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def completion_json_to_sse(payload: dict[str, Any], phase: AgentPhase | None = None) -> bytes:
    """Wrap a buffered chat.completion JSON as OpenAI-style SSE for stream=true clients."""
    chunks: list[str] = []
    cid = payload.get("id") or f"chatcmpl-{secrets.token_hex(12)}"
    base = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": payload.get("created", int(time.time())),
        "model": payload.get("model", "model.gguf"),
    }
    choice = payload["choices"][0]
    msg = choice.get("message", {})
    has_tools = bool(msg.get("tool_calls"))

    role_chunk = _sse_chunk(base, {"role": "assistant"}, finish=None)
    if role_chunk:
        chunks.append(role_chunk)

    content = user_facing_content(msg.get("content"))

    if has_tools:
        for i, tc in enumerate(msg["tool_calls"]):
            fn = tc.get("function", {})
            tool_chunk = _sse_chunk(
                base,
                {
                    "tool_calls": [
                        {
                            "index": i,
                            "id": tc.get("id"),
                            "type": tc.get("type", "function"),
                            "function": {
                                "name": fn.get("name", ""),
                                "arguments": fn.get("arguments", ""),
                            },
                        }
                    ]
                },
                finish=None,
            )
            if tool_chunk:
                chunks.append(tool_chunk)
    else:
        # Prose before inspector so Cursor chat shows the answer first.
        if content:
            content_chunk = _sse_chunk(base, {"content": content}, finish=None)
            if content_chunk:
                chunks.append(content_chunk)

    end_chunk = _sse_chunk(base, {}, finish=choice.get("finish_reason", "stop"))
    if end_chunk:
        chunks.append(end_chunk)
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks).encode("utf-8")


def _infer_tool_score(tool_name: str, query: str, system_content: str) -> float:
    """Heuristic score: how likely was this tool selected given query signals."""
    q = query.lower()
    s = system_content.lower()
    score = 0.0
    if tool_name == "Read":
        for kw in ["읽", "파일", "확인", "read", ".yml", ".py", ".json", ".md", ".sh"]:
            if kw in q: score += 0.15
        if "read" in s: score += 0.1
    elif tool_name == "Shell":
        for kw in ["실행", "curl", "bash", "run", "docker", "command", "테스트", "검증", "shell"]:
            if kw in q: score += 0.15
        if "shell" in s or "must call shell" in s: score += 0.2
    elif tool_name == "Grep":
        for kw in ["grep", "찾", "search", "검색", "위치", "패턴", "코드에서"]:
            if kw in q: score += 0.15
        if "grep" in s: score += 0.1
    return min(round(score, 2), 1.0)


def build_tool_selection_trace(
    response: dict[str, Any],
    available_tools: list[dict[str, Any]] | None,
    query: str,
    proxy_messages: list[dict[str, Any]] | None,
    intent: str = "",
    phase: str = "",
) -> dict[str, Any]:
    """Analyze tool selection decision and return a structured trace."""
    selected_tcs = []
    try:
        selected_tcs = response["choices"][0]["message"].get("tool_calls") or []
    except (KeyError, IndexError, TypeError):
        pass

    selected_names = [tc.get("function", {}).get("name", "?") for tc in selected_tcs]
    available_names = [t["function"]["name"] for t in (available_tools or [])]

    system_content = ""
    if proxy_messages:
        for m in proxy_messages:
            if m.get("role") == "system":
                system_content = str(m.get("content") or "")
                break

    scores: dict[str, float] = {
        name: _infer_tool_score(name, query, system_content)
        for name in (available_names or selected_names)
    }

    skipped = [n for n in available_names if n not in selected_names]

    # Infer reason for each skipped tool
    reasons: dict[str, str] = {}
    q = query.lower()
    has_sys = bool(system_content)
    has_shell_hint = "shell" in system_content.lower() or "must call shell" in system_content.lower()
    for name in skipped:
        if name == "Shell":
            if not has_sys:
                reasons[name] = "system_prompt_missing → no Shell instruction reached model"
            elif not has_shell_hint:
                reasons[name] = "system prompt present but no explicit Shell mandate"
            elif "실행" not in q and "curl" not in q and "run" not in q:
                reasons[name] = "query lacks explicit execution keyword"
            else:
                reasons[name] = "model prioritized sequential read-first strategy"
        elif name == "Grep":
            if "찾" not in q and "grep" not in q and "검색" not in q and "위치" not in q:
                reasons[name] = "query does not mention search/pattern task"
            else:
                reasons[name] = "model chose Read over Grep for this query"
        else:
            reasons[name] = "not required by this task"

    # Overall decision signal
    signals: list[str] = []
    if not has_sys:
        signals.append("❌ no_system_prompt → Shell/Grep instructions never reached model")
    else:
        signals.append("✅ system_prompt_present")
    if has_shell_hint:
        signals.append("✅ shell_mandate_in_system")
    else:
        signals.append("⚠️  no_shell_mandate_in_system")
    if "curl" in q or "실행" in q:
        signals.append("✅ shell_keyword_in_query")
    if len(selected_names) == 0:
        signals.append("⚠️  no_tools_selected → possible context_pack_sufficient_judgment")

    trace = {
        "intent": intent,
        "phase": phase,
        "available": available_names,
        "selected": selected_names,
        "skipped": skipped,
        "scores": scores,
        "skip_reasons": reasons,
        "signals": signals,
        "system_present": has_sys,
        "shell_mandate": has_shell_hint,
    }
    return trace


def log_tool_selection_trace(trace: dict[str, Any]) -> None:
    selected = trace["selected"]
    skipped = trace["skipped"]
    scores = trace["scores"]

    # Score table line
    score_parts = []
    for name in trace["available"]:
        marker = "●" if name in selected else "○"
        score_parts.append(f"{marker}{name}({scores.get(name, 0.0):.2f})")
    LOG.info(
        "tool_selection intent=%s phase=%s selected=%s skipped=%s | %s",
        trace["intent"],
        trace["phase"],
        selected or "(none)",
        skipped or "(none)",
        "  ".join(score_parts),
    )
    for sig in trace["signals"]:
        LOG.debug("  signal: %s", sig)
    if skipped:
        LOG.debug("  skip_detail: %s", "; ".join(f"{n}={r}" for n, r in trace["skip_reasons"].items()))


def sanitize_agent_response(response: dict[str, Any], phase: AgentPhase | None = None) -> tuple[dict[str, Any], AgentExecLog]:
    log = AgentExecLog(phase=phase or "")

    if has_tool_calls(response):
        log.content_stripped = strip_tool_call_content(response)
        tcs = response["choices"][0]["message"].get("tool_calls", [])
        log.tool_calls = len(tcs)
        first_tool = ""
        if tcs and isinstance(tcs[0], dict):
            fn = tcs[0].get("function", {})
            if isinstance(fn, dict):
                first_tool = str(fn.get("name", ""))
        LOG.info(
            "agent_exec phase=%s content_stripped=%s tool_calls=%d first_tool=%s",
            phase or "tool_planning",
            str(log.content_stripped).lower(),
            log.tool_calls,
            first_tool or "(none)",
        )
        return response, log

    content = get_message_content(response)
    cleaned, changed = sanitize_response_content(content)
    if changed or (content and cleaned != content):
        response["choices"][0]["message"]["content"] = cleaned
        log.content_sanitized = True
    LOG.info(
        "agent_exec phase=%s tool_calls_disabled=%s content_sanitized=%s",
        phase or "final_answer",
        str(phase == "final_answer").lower(),
        str(log.content_sanitized).lower(),
    )
    return response, log


def has_tool_calls(response: dict[str, Any]) -> bool:
    try:
        msg = response["choices"][0]["message"]
        return bool(msg.get("tool_calls"))
    except (KeyError, IndexError, TypeError):
        return False


def get_message_content(response: dict[str, Any]) -> str:
    try:
        return str(response["choices"][0]["message"].get("content") or "")
    except (KeyError, IndexError, TypeError):
        return ""


def detect_markdown_shell(content: str) -> bool:
    return bool(BASH_FENCE_RE.search(content))


def extract_bash_commands(content: str) -> list[str]:
    cmds: list[str] = []
    for m in BASH_FENCE_RE.finditer(content):
        block = m.group(1).strip()
        for line in block.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                cmds.append(line)
                break
    return cmds


def is_allowlisted_command(command: str) -> bool:
    cmd = command.strip()
    return any(p.match(cmd) for p in ALLOWLIST_PATTERNS)


def run_allowlisted_command(command: str, cwd: str = "/home/yunahe/ai-runtime/cursor-local-llm") -> tuple[int, str]:
    if not is_allowlisted_command(command):
        return 127, f"command not allowlisted: {command}"
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode, out[:8000]
    except Exception as exc:
        return 1, str(exc)


def strip_xml_tool_blocks(content: str) -> str:
    """Remove leaked XML tool_call blocks from final_answer prose."""
    if not content:
        return ""
    cleaned = XML_TOOL_BLOCK_RE.sub("", content).strip()
    cleaned = re.sub(r"</?tool_call>", "", cleaned, flags=re.I).strip()
    return cleaned


def _emit_final_rejected(reason: str) -> None:
    try:
        from adapters.observe import current_run_id, emit_task

        rid = current_run_id()
        if rid:
            emit_task(rid, "final.rejected", reason[:240])
    except ImportError:
        pass


def guard_final_answer_content(response: dict[str, Any]) -> tuple[bool, bool]:
    """Strip XML tool_call leaks from final_answer content.

    Returns (modified, insufficient_prose).
    """
    try:
        msg = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return False, False
    content = str(msg.get("content") or "")
    if not content:
        return False, False
    if "<function=" not in content.lower() and "<tool_call>" not in content.lower():
        return False, False
    original_len = len(content)
    cleaned = strip_xml_tool_blocks(content)
    insufficient = not cleaned or len(cleaned.strip()) < MIN_FINAL_CHARS
    if insufficient:
        return True, True
    msg["content"] = cleaned
    LOG.warning(
        "final_answer xml_tool_leak stripped original_len=%d cleaned_len=%d",
        original_len,
        len(cleaned),
    )
    return True, False


def parse_function_xml(content: str) -> tuple[str, dict[str, str]] | None:
    """Parse Qwen-style <function=Tool><parameter=key>value blocks."""
    if not content or "<function=" not in content.lower():
        return None
    m = FUNCTION_XML_RE.search(content)
    if not m:
        return None
    tool_name = m.group(1)
    tail = content[m.end() :]
    args: dict[str, str] = {}
    for pm in PARAMETER_XML_RE.finditer(tail):
        key = pm.group(1).strip()
        val = pm.group(2).strip()
        if key and val:
            args[key] = val
    if not args:
        return None
    return tool_name, args


def _finish_agent_response(
    response: dict[str, Any],
    log: AgentExecLog,
    *,
    phase: AgentPhase | None,
    intent_name: str,
    query: str,
    plan: Any | None = None,
    session_state: Any | None = None,
    reason: str = "",
) -> tuple[dict[str, Any], AgentExecLog]:
    from .response_guard import apply_nonempty_guard

    norm_phase = phase or "tool_planning"
    out = normalize_client_response(response, phase=norm_phase)
    out, guarded = apply_nonempty_guard(
        out,
        phase=norm_phase,
        intent_name=intent_name,
        query=query,
        plan=plan,
        session_state=session_state,
        reason=reason,
    )
    if guarded:
        log.fallback = True
    return out, log


def synthetic_tool_calls_response(
    original: dict[str, Any],
    calls: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    out = copy.deepcopy(original)
    tool_calls: list[dict[str, Any]] = []
    for tool_name, args in calls:
        tool_calls.append(
            {
                "id": f"call_router_{secrets.token_hex(8)}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
        )
    msg = {"role": "assistant", "content": "", "tool_calls": tool_calls}
    choice = out.setdefault("choices", [{}])[0]
    choice["message"] = msg
    choice["finish_reason"] = "tool_calls"
    return out


def synthetic_tool_response(
    original: dict[str, Any],
    arguments: dict[str, Any] | str,
    tool_name: str = "Shell",
) -> dict[str, Any]:
    out = copy.deepcopy(original)
    tc_id = f"call_router_{secrets.token_hex(8)}"
    if isinstance(arguments, str):
        args_dict: dict[str, Any] = {"command": arguments}
    else:
        args_dict = arguments
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tc_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(args_dict, ensure_ascii=False),
                },
            }
        ],
    }
    choice = out.setdefault("choices", [{}])[0]
    choice["message"] = msg
    choice["finish_reason"] = "tool_calls"
    return out


def fallback_response(original: dict[str, Any], command: str | None = None) -> dict[str, Any]:
    out = copy.deepcopy(original)
    if command:
        text = (
            "모델이 tool_call을 생성하지 못했습니다. 아래 명령을 직접 실행하세요.\n\n"
            f"```bash\n{command}\n```"
        )
    else:
        text = "모델이 tool_call을 생성하지 못했습니다. Shell 도구로 직접 확인이 필요합니다."
    out["choices"][0]["message"] = {"role": "assistant", "content": text}
    out["choices"][0]["finish_reason"] = "stop"
    return out


def guard_tool_calls_in_response(
    response: dict[str, Any],
    session_state: Any | None,
) -> dict[str, Any]:
    """Executor guard: validate tool_calls against saved AgentPlan before execution."""
    if not session_state or not getattr(session_state, "agent_plan", None):
        return response
    if not has_tool_calls(response):
        return response
    try:
        from .planner import AgentPlan, validate_tool_call

        ap = AgentPlan.from_dict(session_state.agent_plan)
    except Exception:
        return response
    try:
        msg = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return response
    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        return response

    out_tcs: list[dict[str, Any]] = []
    rejections: list[str] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            out_tcs.append(tc)
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        ok, reason = validate_tool_call(name, args, ap)
        if ok:
            out_tcs.append(tc)
        else:
            rejections.append(f"{name}: {reason}")
            LOG.warning("tool_call_guard_reject tool=%s reason=%s", name, reason)
            if str(getattr(ap, "router_intent", "") or "") == "read_only_analysis":
                try:
                    from explorer_trace import trace_explorer_action

                    trace_explorer_action(
                        "action_blocked",
                        tool=name,
                        source_id=str(args.get("source_id") or args.get("_source_id") or ""),
                        pattern=str(args.get("pattern") or ""),
                        glob_pattern=str(args.get("glob_pattern") or ""),
                        guard_reason=reason,
                    )
                except ImportError:
                    pass
            if name == "Read" and ("large_file" in reason or "json_log" in reason):
                path = str(args.get("path") or "")
                try:
                    from .read_guard import grep_instead_of_read, is_large_json_log_path

                    if path and is_large_json_log_path(path):
                        g = grep_instead_of_read(path)
                        out_tcs.append(
                            {
                                "id": f"call_guard_{secrets.token_hex(8)}",
                                "type": "function",
                                "function": {
                                    "name": "Grep",
                                    "arguments": json.dumps(g["args"], ensure_ascii=False),
                                },
                            }
                        )
                        LOG.info("read_guard redirect Read -> Grep path=%r", path[:80])
                        continue
                except ImportError:
                    pass
            try:
                from adapters.observe import current_run_id, emit_tool_call

                rid = current_run_id()
                if rid:
                    emit_tool_call(
                        rid,
                        call_id=str(tc.get("id") or f"guard_{secrets.token_hex(4)}"),
                        name=name,
                        status="error",
                        args=args,
                        guard_reason=reason,
                    )
            except ImportError:
                pass

    msg["tool_calls"] = out_tcs
    if rejections and not out_tcs:
        if ap.final_ready and all("final_ready active" in r for r in rejections):
            from .response_guard import build_partial_final_prose

            msg.pop("tool_calls", None)
            msg["content"] = build_partial_final_prose(
                ap.goal or "",
                plan=ap,
                session_state=session_state,
            )
            choice = response.get("choices", [{}])[0]
            choice["finish_reason"] = "stop"
            LOG.info("tool_call_guard_promote final_ready_blocked_tools=%d", len(rejections))
            return response
        na = ap.next_action
        hint = ""
        if na:
            hint = f" Suggested: {na.get('tool')} {na.get('target', '')}."
        msg["content"] = (
            "Executor guard blocked tool call(s):\n"
            + "\n".join(f"- {r}" for r in rejections)
            + hint
        )
    return response


def redirect_blocked_tool_calls(
    response: dict[str, Any],
    plan: Any | None,
    workspace: str = "",
) -> dict[str, Any]:
    """Replace blocked Read tool_calls with Shell validation when possible."""
    if not plan or not has_tool_calls(response):
        return response
    from .plan_state import is_read_blocked, validation_shell_for_path

    try:
        msg = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return response
    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        return response

    out_tcs: list[dict[str, Any]] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            out_tcs.append(tc)
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "")
        if name != "Read":
            out_tcs.append(tc)
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        path = str(args.get("path") or "")
        if not is_read_blocked(plan, path, workspace):
            out_tcs.append(tc)
            continue
        shell_cmd = validation_shell_for_path(path)
        if shell_cmd and is_allowlisted_command(shell_cmd):
            out_tcs.append(
                {
                    "id": f"call_router_{secrets.token_hex(8)}",
                    "type": "function",
                    "function": {
                        "name": "Shell",
                        "arguments": json.dumps({"command": shell_cmd}, ensure_ascii=False),
                    },
                }
            )
            LOG.info("redirect_blocked_read path=%r -> Shell validation", path[:80])
        else:
            LOG.info("redirect_blocked_read path=%r -> dropped", path[:80])

    msg["tool_calls"] = out_tcs
    if not out_tcs:
        msg["content"] = msg.get("content") or ""
    return response


def planner_commands(intent_name: str, query: str) -> list[str]:
    """Lightweight command planner for router-side execution fallback."""
    cmds: list[str] = []
    if intent_name in ("benchmark", "log_analysis", "shell_task"):
        cmds.append("docker logs cursor-local-llm-router --tail 30")
        cmds.append("python3 scripts/benchmark-router-live.py")
    if "터미널" in query or "명령" in query:
        cmds.append("head -n 8 /home/yunahe/.cursor/projects/home-yunahe/terminals/*.txt 2>/dev/null | tail -40")
    return [c for c in cmds if is_allowlisted_command(c)]


def strip_final_answer_tool_calls(
    response: dict[str, Any],
    plan: Any | None,
    query: str,
) -> bool:
    """Remove JSON tool_calls in final_answer; replace with evidence-based prose."""
    if not has_tool_calls(response):
        return False
    try:
        msg = response["choices"][0]["message"]
        choice = response["choices"][0]
    except (KeyError, IndexError, TypeError):
        return False
    leaked = [
        (tc.get("function") or {}).get("name", "")
        for tc in (msg.get("tool_calls") or [])
        if isinstance(tc, dict)
    ]
    LOG.warning("final_answer json tool_call leak blocked tools=%s", leaked)
    msg.pop("tool_calls", None)
    choice["finish_reason"] = "stop"
    from .plan_state import build_evidence_answer

    answer = build_evidence_answer(plan, query) if plan else ""
    if not answer:
        answer = (
            "final_answer 단계에서는 tool을 호출할 수 없습니다. "
            "이미 수집된 evidence를 바탕으로 prose 답변만 제공합니다."
        )
    msg["content"] = answer
    return True


def _session_agent_plan(session_state: Any | None) -> Any | None:
    if not session_state or not getattr(session_state, "agent_plan", None):
        return None
    try:
        from .planner import AgentPlan

        return AgentPlan.from_dict(session_state.agent_plan)
    except Exception:
        return None


# #region agent log
_DEBUG_LOG_PATH = "/home/yunahe/.cursor/debug-694f50.log"


def _agent_debug_log(
    location: str,
    message: str,
    data: dict[str, Any],
    *,
    hypothesis_id: str = "",
    run_id: str = "pre-fix",
) -> None:
    try:
        payload = {
            "sessionId": "694f50",
            "location": location,
            "message": message,
            "data": data,
            "hypothesisId": hypothesis_id,
            "runId": run_id,
            "timestamp": int(time.time() * 1000),
        }
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


# #endregion


def _read_only_exploration_ready(
    ap: Any,
    session_state: Any | None,
    query: str,
) -> tuple[bool, Any | None, Any | None]:
    """True when explorer checklist/depth says synthesis — not another tool round."""
    if str(getattr(ap, "router_intent", "") or "") != "read_only_analysis":
        return False, None, ap
    try:
        from .planner import AgentPlan, can_final_answer
        from .read_only_explorer import (
            READ_ONLY_EXPLORER_ENABLED,
            exploration_checklist_pending,
            refresh_read_only_exploration_plan,
        )
    except ImportError:
        return False, None, ap

    decision = None
    if READ_ONLY_EXPLORER_ENABLED:
        decision = refresh_read_only_exploration_plan(session_state, ap, query)
        if session_state is not None and getattr(session_state, "agent_plan", None):
            ap = AgentPlan.from_dict(session_state.agent_plan)

    pending = exploration_checklist_pending(ap.to_dict(), ap.source_registry)
    ready = bool(
        (decision and decision.allow_final)
        or not pending
        or can_final_answer(ap)
    )
    # #region agent log
    _agent_debug_log(
        "agent_exec.py:_read_only_exploration_ready",
        "read_only_final_gate",
        {
            "ready": ready,
            "allow_final": bool(decision and decision.allow_final),
            "pending_count": len(pending),
            "pending_head": pending[:6],
            "can_final": can_final_answer(ap),
            "thinking_len": len(str(getattr(ap, "exploration_thinking", "") or "")),
        },
        hypothesis_id="A",
    )
    # #endregion
    return ready, decision, ap


def _build_read_only_final_retry_body(
    retry_body: dict[str, Any],
    ap: Any,
    query: str,
    decision: Any | None,
    session_state: Any | None,
) -> dict[str, Any]:
    from chat_fast import strip_agent_fields

    body = copy.deepcopy(retry_body)
    strip_agent_fields(body)

    thinking = str(getattr(ap, "exploration_thinking", "") or "").strip()
    if decision is not None:
        thinking = thinking or str(getattr(decision, "thinking", "") or "").strip()

    digest_rows: list[dict[str, Any]] = []
    try:
        from .read_only_explorer import _source_digests

        if session_state is not None:
            digest_rows = _source_digests(session_state, ap, limit=16)
    except ImportError:
        pass

    sys_parts = [
        build_runtime_system("read_only_analysis", phase="final_answer"),
        "\nUse the explorer thinking and tier digests below — synthesize in Korean with tables, "
        "line citations, and component relationships. Do NOT emit tool_calls.",
    ]
    if thinking:
        sys_parts.append(f"\n[Explorer thinking]\n{thinking[:1800]}")

    user_parts = [f"[User query]\n{query.strip() or str(getattr(ap, 'goal', '') or '')}"]
    if digest_rows:
        user_parts.append("\n[Tier evidence digests]")
        for row in digest_rows:
            sid = str(row.get("source_id") or "")
            tool = str(row.get("tool") or "digest")
            digest = str(row.get("digest") or "").strip()
            if sid and digest:
                user_parts.append(f"\n### {sid} ({tool})\n{digest[:1400]}")
    elif getattr(ap, "source_digests", None):
        user_parts.append("\n[Tier evidence digests — plan cache]")
        for sid, digest in list((ap.source_digests or {}).items())[:12]:
            if str(digest).strip():
                user_parts.append(f"\n### {sid}\n{str(digest).strip()[:1200]}")

    body["messages"] = [
        {"role": "system", "content": "\n".join(sys_parts)},
        {"role": "user", "content": "\n".join(user_parts)},
    ]
    apply_model_request_opts(body, "read_only_analysis", "final_answer", query=query)
    return body


def _try_promote_read_only_final_synthesis(
    response: dict[str, Any],
    log: AgentExecLog,
    *,
    intent_name: str,
    query: str,
    plan: Any | None,
    session_state: Any | None,
    retry_call: Callable[[dict[str, Any]], dict[str, Any]] | None,
    retry_body: dict[str, Any] | None,
    proxy_messages: list[dict[str, Any]] | None,
    promote_depth: int,
) -> tuple[dict[str, Any], AgentExecLog] | None:
    if promote_depth > 0 or intent_name != "read_only_analysis":
        return None
    if retry_call is None or retry_body is None:
        # #region agent log
        _agent_debug_log(
            "agent_exec.py:_try_promote_read_only_final_synthesis",
            "promote_skipped_no_retry",
            {"has_retry_call": retry_call is not None, "has_retry_body": retry_body is not None},
            hypothesis_id="D",
        )
        # #endregion
        return None

    ap = _session_agent_plan(session_state)
    if ap is None:
        return None

    ready, decision, ap = _read_only_exploration_ready(ap, session_state, query)
    if not ready:
        return None

    try:
        from .planner import mark_final_ready

        mark_final_ready(ap, query=query, project_root=getattr(session_state, "workspace_path", "") or "")
        if session_state is not None:
            session_state.agent_plan = ap.to_dict()
    except ImportError:
        pass

    try:
        from explorer_trace import trace_explorer_action

        trace_explorer_action(
            "final_promote",
            tool="answer",
            source_id="",
            pattern="",
            glob_pattern="",
            override=False,
            action_sig="final:inline_synthesis",
            thinking=str(getattr(ap, "exploration_thinking", "") or "")[:400],
        )
    except ImportError:
        pass

    final_body = _build_read_only_final_retry_body(retry_body, ap, query, decision, session_state)
    log.retry = 1
    LOG.info(
        "read_only_explorer inline_final_synthesis digests=%d thinking_len=%d",
        len(getattr(ap, "source_digests", None) or {}),
        len(str(getattr(ap, "exploration_thinking", "") or "")),
    )
    # #region agent log
    _agent_debug_log(
        "agent_exec.py:_try_promote_read_only_final_synthesis",
        "inline_final_llm_call",
        {
            "digest_rows": len(final_body.get("messages") or []),
            "max_tokens": final_body.get("max_tokens"),
        },
        hypothesis_id="D",
        run_id="post-fix",
    )
    # #endregion
    final_resp = retry_call(final_body)
    return postprocess_agent_response(
        final_resp,
        intent_name,
        query,
        phase="final_answer",
        session_state=session_state,
        promote_depth=promote_depth + 1,
    )


def postprocess_agent_response(
    response: dict[str, Any],
    intent_name: str,
    query: str,
    retry_call: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    retry_body: dict[str, Any] | None = None,
    phase: AgentPhase | None = "tool_planning",
    available_tools: list[dict[str, Any]] | None = None,
    proxy_messages: list[dict[str, Any]] | None = None,
    session_state: Any | None = None,
    promote_depth: int = 0,
) -> tuple[dict[str, Any], AgentExecLog]:
    log = AgentExecLog(intent=intent_name, phase=phase or "tool_planning")
    plan = None
    workspace = ""
    if session_state is not None:
        from .plan_state import build_plan_state

        workspace = getattr(session_state, "workspace_path", "") or ""
        plan = build_plan_state(session_state, query, messages=proxy_messages)

    if phase in ("final_answer", "partial_final_answer", "recovery_final"):
        if has_tool_calls(response):
            log.content_sanitized = True
            strip_final_answer_tool_calls(response, plan, query)

        content = get_message_content(response)
        from .response_guard import build_partial_final_prose, parse_tool_call_content

        xml_parsed = parse_tool_call_content(content)
        has_xml_leak = bool(xml_parsed) or "<tool_call>" in content.lower() or "<function=" in content.lower() or "<|tool_start|>" in content.lower()

        if phase == "final_answer" and has_xml_leak and xml_parsed:
            tool_name, args = xml_parsed
            _emit_final_rejected("xml_tool_leak_promote_tool_planning")
            if session_state is not None:
                try:
                    from .loop_guard import record_xml_leak

                    record_xml_leak(session_state)
                except ImportError:
                    pass
            LOG.warning(
                "final_answer xml_tool_leak promote tool=%s args=%r",
                tool_name,
                str(args)[:120],
            )
            log.synthetic_tool_call = True
            return _finish_agent_response(
                synthetic_tool_response(response, args, tool_name=tool_name),
                log,
                phase="tool_planning",
                intent_name=intent_name,
                query=query,
                plan=plan,
                session_state=session_state,
            )

        if has_xml_leak and not xml_parsed:
            msg = response["choices"][0]["message"]
            msg["content"] = build_partial_final_prose(
                query,
                plan=plan,
                session_state=session_state,
                reason="xml_parse_failure",
            )
            msg.pop("tool_calls", None)
            log.content_sanitized = True
            return _finish_agent_response(
                response,
                log,
                phase=phase,
                intent_name=intent_name,
                query=query,
                plan=plan,
                session_state=session_state,
                reason="xml_parse_failure",
            )

        if phase in ("partial_final_answer", "recovery_final") and not content.strip():
            response["choices"][0]["message"]["content"] = build_partial_final_prose(
                query,
                plan=plan,
                session_state=session_state,
                reason="bad_ping_pong" if phase == "partial_final_answer" else "",
            )
            log.content_sanitized = True

        response, sani = sanitize_agent_response(response, phase="final_answer")
        log.content_sanitized = log.content_sanitized or sani.content_sanitized
        modified, insufficient = guard_final_answer_content(response)
        if modified:
            log.content_sanitized = True
        if insufficient and phase == "final_answer":
            raw_content = content or get_message_content(response)
            if session_state is not None:
                try:
                    from .loop_guard import record_xml_leak

                    record_xml_leak(session_state)
                except ImportError:
                    pass
            xml_parsed = parse_tool_call_content(raw_content)
            if xml_parsed:
                tool_name, args = xml_parsed
                _emit_final_rejected("xml_tool_leak_insufficient_prose")
                log.synthetic_tool_call = True
                return _finish_agent_response(
                    synthetic_tool_response(response, args, tool_name=tool_name),
                    log,
                    phase="tool_planning",
                    intent_name=intent_name,
                    query=query,
                    plan=plan,
                    session_state=session_state,
                )
            _emit_final_rejected("xml_tool_leak_insufficient_prose")
            LOG.warning("final_answer insufficient prose after xml strip len=%d", len(raw_content))
            response["choices"][0]["message"]["content"] = build_partial_final_prose(
                query,
                plan=plan,
                session_state=session_state,
                reason="xml_parse_failure",
            )
        return _finish_agent_response(
            response,
            log,
            phase=phase,
            intent_name=intent_name,
            query=query,
            plan=plan,
            session_state=session_state,
            reason="partial_final" if phase == "partial_final_answer" else "",
        )

    if (
        phase == "tool_planning"
        and plan
        and getattr(plan, "task_kind", "") == "compose_port"
        and getattr(plan, "phase", "") == "final_answer_ready"
        and has_tool_calls(response)
    ):
        log.content_sanitized = True
        strip_final_answer_tool_calls(response, plan, query)
        return _finish_agent_response(
            response,
            log,
            phase="final_answer",
            intent_name=intent_name,
            query=query,
            plan=plan,
            session_state=session_state,
        )

    if phase == "tool_planning" and intent_name == "read_only_analysis":
        ap_obj = _session_agent_plan(session_state)
        if ap_obj is None and plan is not None:
            ap_obj = plan
        if ap_obj is not None:
            from .read_only_explorer import (
                READ_ONLY_EXPLORER_ENABLED,
                READ_ONLY_EXPLORER_OVERRIDE,
                get_exploration_tool_call,
            )

            if READ_ONLY_EXPLORER_ENABLED:
                pick = get_exploration_tool_call(
                    ap_obj,
                    session_state,
                    query or str(getattr(ap_obj, "goal", "") or ""),
                )
                if not pick:
                    promoted = _try_promote_read_only_final_synthesis(
                        response,
                        log,
                        intent_name=intent_name,
                        query=query,
                        plan=plan,
                        session_state=session_state,
                        retry_call=retry_call,
                        retry_body=retry_body,
                        proxy_messages=proxy_messages,
                        promote_depth=promote_depth,
                    )
                    if promoted is not None:
                        return promoted
                if pick and (READ_ONLY_EXPLORER_OVERRIDE or not has_tool_calls(response)):
                    log.synthetic_tool_call = True
                    try:
                        from explorer_trace import trace_explorer_action
                        from .read_only_explorer import exploration_action_sig

                        trace_explorer_action(
                            "action_emit",
                            tool=pick[0],
                            source_id=str(pick[1].get("source_id") or ""),
                            pattern=str(pick[1].get("pattern") or ""),
                            glob_pattern=str(pick[1].get("glob_pattern") or ""),
                            override=READ_ONLY_EXPLORER_OVERRIDE,
                            action_sig=exploration_action_sig(
                                pick[0],
                                str(pick[1].get("source_id") or ""),
                                pattern=str(pick[1].get("pattern") or ""),
                                glob_pattern=str(pick[1].get("glob_pattern") or ""),
                            ),
                        )
                    except ImportError:
                        pass
                    LOG.debug(
                        "read_only_explorer_tool tool=%s source_id=%s pattern=%s glob=%s override=%s",
                        pick[0],
                        pick[1].get("source_id"),
                        pick[1].get("pattern", ""),
                        pick[1].get("glob_pattern", ""),
                        READ_ONLY_EXPLORER_OVERRIDE,
                    )
                    response = synthetic_tool_calls_response(response, [pick])

    if has_tool_calls(response):
        from .source_tools import (
            dedupe_identical_tool_calls,
            expand_source_tool_calls_in_response,
            filter_redundant_source_tool_calls,
        )

        ap_obj = _session_agent_plan(session_state)
        if ap_obj is not None:
            response = expand_source_tool_calls_in_response(response, ap_obj)
            response, _deduped = dedupe_identical_tool_calls(response)
            response, _removed_dup = filter_redundant_source_tool_calls(response, ap_obj)
            if _removed_dup and not has_tool_calls(response) and session_state is not None:
                try:
                    from .planner import AgentPlan, can_final_answer
                    from .read_only_explorer import get_exploration_tool_call

                    ap_check = (
                        AgentPlan.from_dict(session_state.agent_plan)
                        if getattr(session_state, "agent_plan", None)
                        else None
                    )
                    if ap_check and str(getattr(ap_check, "router_intent", "") or "") == "read_only_analysis":
                        if not can_final_answer(ap_check):
                            pick = get_exploration_tool_call(
                                ap_check,
                                session_state,
                                query or str(getattr(ap_check, "goal", "") or ""),
                            )
                            if pick:
                                log.synthetic_tool_call = True
                                response = synthetic_tool_calls_response(response, [pick])
                                response = expand_source_tool_calls_in_response(response, ap_check)
                    elif ap_check and can_final_answer(ap_check):
                        from .response_guard import build_partial_final_prose

                        msg = response["choices"][0]["message"]
                        msg.pop("tool_calls", None)
                        msg["content"] = build_partial_final_prose(
                            query,
                            plan=plan,
                            session_state=session_state,
                            reason="",
                        )
                        log.content_sanitized = True
                        return _finish_agent_response(
                            response,
                            log,
                            phase="final_answer",
                            intent_name=intent_name,
                            query=query,
                            plan=plan,
                            session_state=session_state,
                            reason="redundant_tools_coverage_ok",
                        )
                except ImportError:
                    pass
        response = guard_tool_calls_in_response(response, session_state)
        try:
            from adapters.observe import current_run_id, emit_tool_call

            rid = current_run_id()
            if rid:
                msg = response.get("choices", [{}])[0].get("message", {})
                for tc in msg.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    tname = str(fn.get("name") or "")
                    try:
                        targs = json.loads(fn.get("arguments") or "{}")
                    except (json.JSONDecodeError, TypeError):
                        targs = {}
                    emit_tool_call(
                        rid,
                        call_id=str(tc.get("id") or f"tc_{secrets.token_hex(4)}"),
                        name=tname,
                        status="running",
                        args=targs,
                    )
        except (ImportError, KeyError, IndexError, TypeError):
            pass
        response = redirect_blocked_tool_calls(response, plan, workspace)
        if intent_name == "read_only_analysis" and phase == "tool_planning" and has_tool_calls(response):
            try:
                msg = response["choices"][0]["message"]
                tcs = msg.get("tool_calls") or []
                if len(tcs) > 1:
                    msg["tool_calls"] = tcs[:1]
                    LOG.info("read_only_explorer single_tool enforced was=%d", len(tcs))
            except (KeyError, IndexError, TypeError):
                pass
        if not has_tool_calls(response):
            ap_ro = _session_agent_plan(session_state)
            if ap_ro is not None and str(getattr(ap_ro, "router_intent", "") or "") == "read_only_analysis":
                from .read_only_explorer import READ_ONLY_EXPLORER_ENABLED, get_exploration_tool_call

                if READ_ONLY_EXPLORER_ENABLED:
                    pick = get_exploration_tool_call(
                        ap_ro,
                        session_state,
                        query or str(getattr(ap_ro, "goal", "") or ""),
                    )
                    if pick:
                        log.synthetic_tool_call = True
                        response = synthetic_tool_calls_response(response, [pick])
                        response = expand_source_tool_calls_in_response(response, ap_ro)
                    else:
                        promoted = _try_promote_read_only_final_synthesis(
                            response,
                            log,
                            intent_name=intent_name,
                            query=query,
                            plan=plan,
                            session_state=session_state,
                            retry_call=retry_call,
                            retry_body=retry_body,
                            proxy_messages=proxy_messages,
                            promote_depth=promote_depth,
                        )
                        if promoted is not None:
                            return promoted
        if ap_obj is not None and has_tool_calls(response):
            response = expand_source_tool_calls_in_response(response, ap_obj)
        response, sani = sanitize_agent_response(response, phase="tool_planning")
        log.tool_calls = sani.tool_calls
        log.content_stripped = sani.content_stripped
        # Emit tool selection trace
        if available_tools:
            trace = build_tool_selection_trace(
                response, available_tools, query, proxy_messages,
                intent=intent_name, phase="tool_planning",
            )
            log_tool_selection_trace(trace)
        if not has_tool_calls(response):
            promoted = _try_promote_read_only_final_synthesis(
                response,
                log,
                intent_name=intent_name,
                query=query,
                plan=plan,
                session_state=session_state,
                retry_call=retry_call,
                retry_body=retry_body,
                proxy_messages=proxy_messages,
                promote_depth=promote_depth,
            )
            if promoted is not None:
                return promoted
        return _finish_agent_response(
            response,
            log,
            phase="tool_planning",
            intent_name=intent_name,
            query=query,
            plan=plan,
            session_state=session_state,
        )

    content = get_message_content(response)
    if content.strip() == "TOOL_CALL_UNAVAILABLE":
        response["choices"][0]["message"]["content"] = ""
        log.content_stripped = True
        return _finish_agent_response(
            response,
            log,
            phase="tool_planning",
            intent_name=intent_name,
            query=query,
            plan=plan,
            session_state=session_state,
            reason="tool_unavailable",
        )

    from .response_guard import parse_all_tool_calls_from_content, parse_tool_call_content
    from .source_tools import expand_source_tool_calls_in_response, resolve_xml_tool_args

    all_xml = parse_all_tool_calls_from_content(content)
    if all_xml:
        ap_obj = _session_agent_plan(session_state)
        resolved_calls: list[tuple[str, dict[str, Any]]] = []
        for tool_name, args in all_xml:
            ra = resolve_xml_tool_args(tool_name, dict(args), ap_obj)
            resolved_calls.append((tool_name, ra))
        if resolved_calls:
            log.synthetic_tool_call = True
            LOG.info(
                "agent_exec intent=%s xml_tool_calls=%d first=%s",
                intent_name,
                len(resolved_calls),
                resolved_calls[0][0],
            )
            resp = synthetic_tool_calls_response(response, resolved_calls)
            resp = expand_source_tool_calls_in_response(resp, ap_obj)
            resp = guard_tool_calls_in_response(resp, session_state)
            return _finish_agent_response(
                resp,
                log,
                phase="tool_planning",
                intent_name=intent_name,
                query=query,
                plan=plan,
                session_state=session_state,
            )

    xml_parsed = parse_tool_call_content(content)
    if xml_parsed:
        tool_name, args = xml_parsed
        if tool_name and args:
            if tool_name == "Read" and plan:
                from .plan_state import is_read_blocked, validation_shell_for_path

                path = str(args.get("path") or "")
                if is_read_blocked(plan, path, workspace):
                    shell_cmd = validation_shell_for_path(path)
                    if shell_cmd and is_allowlisted_command(shell_cmd):
                        log.synthetic_tool_call = True
                        LOG.info("agent_exec read_blocked redirect shell path=%r", path[:80])
                        return _finish_agent_response(
                            synthetic_tool_response(response, shell_cmd, tool_name="Shell"),
                            log,
                            phase="tool_planning",
                            intent_name=intent_name,
                            query=query,
                            plan=plan,
                            session_state=session_state,
                        )
            log.synthetic_tool_call = True
            LOG.info(
                "agent_exec intent=%s xml_tool_call=true tool=%s args=%r",
                intent_name,
                tool_name,
                str(args)[:120],
            )
            return _finish_agent_response(
                synthetic_tool_response(response, args, tool_name=tool_name),
                log,
                phase="tool_planning",
                intent_name=intent_name,
                query=query,
                plan=plan,
                session_state=session_state,
            )

    if not detect_markdown_shell(content) and content.strip():
        if (
            "<tool_call>" in content.lower()
            or "<function=" in content.lower()
            or "<|tool_start|>" in content.lower()
            or "<tool_code>" in content.lower()
        ):
            log.fallback = True
            return _finish_agent_response(
                response,
                log,
                phase="tool_planning",
                intent_name=intent_name,
                query=query,
                plan=plan,
                session_state=session_state,
                reason="xml_parse_failure",
            )
        json_calls = parse_all_tool_calls_from_content(content)
        if json_calls:
            ap_obj = _session_agent_plan(session_state)
            resolved_calls: list[tuple[str, dict[str, Any]]] = []
            for tool_name, args in json_calls:
                from .source_tools import resolve_xml_tool_args

                ra = resolve_xml_tool_args(tool_name, dict(args), ap_obj)
                resolved_calls.append((tool_name, ra))
            log.synthetic_tool_call = True
            LOG.info(
                "agent_exec intent=%s json_tool_calls=%d first=%s",
                intent_name,
                len(resolved_calls),
                resolved_calls[0][0],
            )
            resp = synthetic_tool_calls_response(response, resolved_calls)
            resp = expand_source_tool_calls_in_response(resp, ap_obj)
            resp = guard_tool_calls_in_response(resp, session_state)
            return _finish_agent_response(
                resp,
                log,
                phase="tool_planning",
                intent_name=intent_name,
                query=query,
                plan=plan,
                session_state=session_state,
            )
        response, sani = sanitize_agent_response(response, phase="tool_planning")
        log.content_sanitized = sani.content_sanitized
        response["choices"][0]["message"]["content"] = ""
        return _finish_agent_response(
            response,
            log,
            phase="tool_planning",
            intent_name=intent_name,
            query=query,
            plan=plan,
            session_state=session_state,
        )

    log.markdown_shell_detected = True
    cmds = extract_bash_commands(content)

    # 1) retry once with stricter system
    if retry_call and retry_body is not None:
        log.retry = 1
        retry_body = copy.deepcopy(retry_body)
        msgs = retry_body.get("messages", [])
        if msgs and isinstance(msgs[0], dict):
            msgs[0]["content"] = RETRY_SYSTEM
        retry_resp = retry_call(retry_body)
        if has_tool_calls(retry_resp):
            response, sani = sanitize_agent_response(retry_resp, phase="tool_planning")
            log.tool_calls = sani.tool_calls
            log.content_stripped = sani.content_stripped
            log.retry = 1
            return _finish_agent_response(
                response,
                log,
                phase="tool_planning",
                intent_name=intent_name,
                query=query,
                plan=plan,
                session_state=session_state,
            )
        content = get_message_content(retry_resp)
        cmds = extract_bash_commands(content) or cmds
        response = retry_resp

    # 2) synthetic tool_call from allowlisted bash
    for cmd in cmds:
        if is_allowlisted_command(cmd):
            log.synthetic_tool_call = True
            LOG.info(
                "agent_exec intent=%s tool_calls=0 retry=%d markdown_shell=true synthetic=true cmd=%r",
                intent_name,
                log.retry,
                cmd[:120],
            )
            return _finish_agent_response(
                synthetic_tool_response(response, cmd),
                log,
                phase="tool_planning",
                intent_name=intent_name,
                query=query,
                plan=plan,
                session_state=session_state,
            )
    for cmd in planner_commands(intent_name, query):
        code, out = run_allowlisted_command(cmd)
        if code == 0 and out:
            log.router_executed = True
            log.synthetic_tool_call = True
            LOG.info(
                "agent_exec intent=%s router_executed=true synthetic=true cmd=%r",
                intent_name,
                cmd[:120],
            )
            return _finish_agent_response(
                synthetic_tool_response(response, cmd),
                log,
                phase="tool_planning",
                intent_name=intent_name,
                query=query,
                plan=plan,
                session_state=session_state,
            )

    # 4) fallback
    log.fallback = True
    cmd = cmds[0] if cmds else None
    LOG.info(
        "agent_exec intent=%s tool_calls=0 retry=%d markdown_shell=true synthetic=false fallback=true",
        intent_name,
        log.retry,
    )
    out = fallback_response(response, cmd)
    return _finish_agent_response(
        out,
        log,
        phase="tool_planning",
        intent_name=intent_name,
        query=query,
        plan=plan,
        session_state=session_state,
        reason="markdown_fallback",
    )
