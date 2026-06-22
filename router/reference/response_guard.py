"""Client response invariants — no empty outgoing responses."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

LOG = logging.getLogger("router.response_guard")

PROSE_PHASES = frozenset({"final_answer", "partial_final_answer", "recovery_final"})

QWEN_TOOL_START_RE = re.compile(
    r"<\|tool_start\|>(\w+)<\|tool_sep\|>(.*?)(?=<\|tool_start\|>|</tool_call>|$)",
    re.I | re.S,
)
QWEN_PATH_ARG_RE = re.compile(r"^\s*path\s*=\s*(.+)$", re.I | re.M)
TOOL_CODE_INVOKE_RE = re.compile(
    r"<tool_code>\s*<invoke\s+name=[\"'](\w+)[\"']>(.*?)</invoke>\s*</tool_code>",
    re.I | re.S,
)
TOOL_CODE_PARAM_RE = re.compile(
    r"<parameter\s+name=[\"'](\w+)[\"']>(.*?)</parameter>",
    re.I | re.S,
)
MALFORMED_ARGKV_RE = re.compile(
    r"<tool_call>\s*(\w+)\s*>\s*arg_key_value_list\s*>\s*(\{.*?\})\s*</tool_call>",
    re.I | re.S,
)
MALFORMED_PATH_ARROW_RE = re.compile(
    r"<tool_call>\s*(\w+)\s*>\s*path\s*>\s*([^\s<>]+)",
    re.I,
)


def is_prose_phase(phase: str | None) -> bool:
    return (phase or "") in PROSE_PHASES


def count_outgoing_tool_calls(response: dict[str, Any]) -> int:
    try:
        msg = response["choices"][0]["message"]
        return len(msg.get("tool_calls") or [])
    except (KeyError, IndexError, TypeError):
        return 0


def outgoing_content_chars(response: dict[str, Any]) -> int:
    try:
        msg = response["choices"][0]["message"]
        content = str(msg.get("content") or "")
        if not content.strip() and msg.get("reasoning_content"):
            content = str(msg.get("reasoning_content") or "")
        return len(content.strip())
    except (KeyError, IndexError, TypeError):
        return 0


def is_empty_outgoing(response: dict[str, Any]) -> bool:
    return count_outgoing_tool_calls(response) == 0 and outgoing_content_chars(response) == 0


def parse_qwen_tool_start_xml(content: str) -> tuple[str, dict[str, str]] | None:
    """Parse ``<|tool_start|>Read<|tool_sep|>path=...`` blocks."""
    if not content or "<|tool_start|>" not in content.lower():
        return None
    m = QWEN_TOOL_START_RE.search(content)
    if not m:
        return None
    tool_name = m.group(1).strip()
    tail = m.group(2).strip()
    if not tool_name:
        return None
    args: dict[str, str] = {}
    pm = QWEN_PATH_ARG_RE.search(tail)
    if pm:
        args["path"] = pm.group(1).strip().strip('"').strip("'")
    else:
        for line in tail.splitlines():
            line = line.strip()
            if not line or line.startswith("<"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                args[k.strip()] = v.strip().strip('"').strip("'")
    if tool_name.lower() == "shell" and "command" not in args and tail:
        args["command"] = tail.splitlines()[0].strip()
    if not args:
        return None
    return tool_name, args


def parse_tool_code_invoke(content: str) -> tuple[str, dict[str, str]] | None:
    """Parse ``<tool_code><invoke name="Shell"><parameter name="command">...`` blocks."""
    if not content or "<tool_code>" not in content.lower():
        return None
    m = TOOL_CODE_INVOKE_RE.search(content)
    if not m:
        return None
    tool_name = m.group(1).strip()
    body = m.group(2)
    args: dict[str, str] = {}
    for pm in TOOL_CODE_PARAM_RE.finditer(body):
        key = pm.group(1).strip()
        val = pm.group(2).strip()
        if key and val:
            args[key] = val
    if not tool_name or not args:
        return None
    return tool_name, args


def parse_malformed_argkv_tool_calls(content: str) -> list[tuple[str, dict[str, str]]]:
    """Parse ``<tool_call> Read> arg_key_value_list> {"path": "..."} </tool_call>``."""
    if not content or "arg_key_value_list" not in content.lower():
        return []
    out: list[tuple[str, dict[str, str]]] = []
    for m in MALFORMED_ARGKV_RE.finditer(content):
        tool_name = m.group(1).strip()
        raw_json = m.group(2).strip()
        try:
            import json

            parsed = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        args = {str(k): str(v) for k, v in parsed.items()}
        if tool_name and args:
            out.append((tool_name, args))
    return out


def parse_malformed_path_arrow_tool_calls(content: str) -> list[tuple[str, dict[str, str]]]:
    """Parse ``<tool_call> Read> path>docs/MODULE_MAP.md`` (no JSON, optional chain)."""
    if not content or "path>" not in content.lower():
        return []
    out: list[tuple[str, dict[str, str]]] = []
    for m in MALFORMED_PATH_ARROW_RE.finditer(content):
        tool_name = m.group(1).strip()
        path = m.group(2).strip().strip('"').strip("'").rstrip(">")
        if tool_name and path:
            out.append((tool_name, {"path": path}))
    return out


def parse_json_tool_calls_from_content(content: str) -> list[tuple[str, dict[str, str]]]:
    """Recover OpenAI-style JSON tool_calls leaked into assistant content."""
    text = (content or "").strip()
    if not text or '"tool_calls"' not in text:
        return []
    blob = text
    if not blob.lstrip().startswith("{"):
        start = blob.find("{")
        if start < 0:
            return []
        blob = blob[start:]
    calls: list[Any] = []
    try:
        data = json.loads(blob)
        if isinstance(data, dict):
            raw = data.get("tool_calls")
            if isinstance(raw, list):
                calls = raw
    except json.JSONDecodeError:
        m = re.search(r'"tool_calls"\s*:\s*(\[[\s\S]*?\])\s*[,}]', text)
        if not m:
            return []
        try:
            calls = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []
    out: list[tuple[str, dict[str, str]]] = []
    for call in calls or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(call.get("name") or call.get("tool_name") or fn.get("name") or "")
        args = call.get("arguments")
        if args is None:
            args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        if name:
            out.append((name, {str(k): str(v) for k, v in args.items()}))
    return out


def parse_all_tool_calls_from_content(content: str) -> list[tuple[str, dict[str, str]]]:
    """Collect all recoverable tool calls embedded in prose/XML."""
    from .agent_exec import parse_function_xml

    found: list[tuple[str, dict[str, str]]] = []
    seen: set[str] = set()

    def _add(tool: str, args: dict[str, str]) -> None:
        key = f"{tool}:{json.dumps(args, sort_keys=True)}"
        if key in seen:
            return
        seen.add(key)
        found.append((tool, args))

    for tool, args in parse_json_tool_calls_from_content(content):
        _add(tool, args)

    for tool, args in parse_malformed_argkv_tool_calls(content):
        _add(tool, args)

    for tool, args in parse_malformed_path_arrow_tool_calls(content):
        _add(tool, args)

    parsed = parse_function_xml(content)
    if parsed:
        t, a = parsed
        _add(t, {k: str(v) for k, v in a.items()})

    parsed = parse_qwen_tool_start_xml(content)
    if parsed:
        t, a = parsed
        _add(t, a)

    parsed = parse_tool_code_invoke(content)
    if parsed:
        t, a = parsed
        _add(t, a)

    return found


def parse_tool_call_content(content: str) -> tuple[str, dict[str, str]] | None:
    """Try all parsers; return first match (use parse_all for multiples)."""
    all_calls = parse_all_tool_calls_from_content(content)
    if all_calls:
        return all_calls[0]
    return None


def _evidence_collected(session_state: Any | None, plan: Any | None) -> list[str]:
    if plan is not None and getattr(plan, "evidence_collected", None):
        return list(plan.evidence_collected or [])
    if session_state is not None and getattr(session_state, "agent_plan", None):
        ap = session_state.agent_plan or {}
        if isinstance(ap, dict):
            return list(ap.get("evidence_collected") or [])
    return []


def _mark_final_report_used(
    session_state: Any,
    *,
    used: bool,
    reason: str,
    chars: int,
) -> None:
    rt = dict(getattr(session_state, "last_runtime_turn", None) or {})
    rt["final_report_used"] = used
    rt["final_report_reason"] = reason
    rt["final_report_chars"] = chars
    session_state.last_runtime_turn = rt
    LOG.info(
        "final_report_used=%s reason=%s chars=%d",
        str(used).lower(),
        reason or "n/a",
        chars,
    )
    try:
        from explorer_trace import write_explorer_trace

        write_explorer_trace(
            "final_report.rendered",
            phase=str(getattr(session_state, "phase_hint", "") or ""),
            query=str(getattr(session_state, "current_query", "") or "")[:500],
            turn_index=int(getattr(session_state, "turn_index", 0) or 0),
            decision="final_report" if used else "fallback",
            result_summary=f"used={used} chars={chars} reason={reason}",
            final_report_used=used,
            final_report_chars=chars,
        )
    except Exception:
        pass


def build_partial_final_prose(
    query: str,
    *,
    plan: Any | None = None,
    session_state: Any | None = None,
    reason: str = "",
) -> str:
    """Deterministic prose when the agent loop must stop with partial evidence."""
    if session_state is not None:
        try:
            from runtime_kernel.final_report import render_final_report

            report = render_final_report(session_state, query=query)
            if report and len(report.strip()) > 200:
                _mark_final_report_used(session_state, used=True, reason=reason, chars=len(report))
                prefix = ""
                if reason == "bad_ping_pong":
                    prefix = (
                        "현재 탐색 루프가 반복되어 추가 도구 호출을 중단했습니다. "
                        "확보된 근거 기준으로 요약합니다.\n\n"
                    )
                elif reason == "xml_parse_failure":
                    prefix = (
                        "모델이 도구 호출 형식으로 응답했지만 파싱에 실패했습니다. "
                        "수집된 evidence 기준으로 부분 답변을 제공합니다.\n\n"
                    )
                elif reason in ("empty_outgoing", "explorer_checklist_complete"):
                    prefix = (
                        "탐색 checklist가 완료되어 수집된 근거 기준으로 답변합니다.\n\n"
                    )
                elif reason:
                    prefix = "요청 처리 중 도구 루프를 종료하고 확보된 근거로 답변합니다.\n\n"
                return (prefix + report).strip()
            _mark_final_report_used(session_state, used=False, reason=reason, chars=len(report or ""))
        except Exception as exc:
            _mark_final_report_used(session_state, used=False, reason=f"error:{exc}", chars=0)

    lines: list[str] = []
    if reason == "bad_ping_pong":
        lines.append(
            "현재 탐색 루프가 반복되어 추가 도구 호출을 중단했습니다. "
            "확보된 근거 기준으로 요약합니다."
        )
    elif reason == "xml_parse_failure":
        lines.append(
            "모델이 도구 호출 형식으로 응답했지만 파싱에 실패했습니다. "
            "수집된 evidence 기준으로 부분 답변을 제공합니다."
        )
    elif reason in ("empty_outgoing", "explorer_checklist_complete"):
        lines.append(
            "탐색 checklist가 완료되어 수집된 tier digest와 explorer thinking 기준으로 답변합니다."
        )
    else:
        lines.append("요청 처리 중 도구 루프를 종료하고 확보된 근거로 답변합니다.")

    ap_ro: Any | None = None
    if session_state is not None and getattr(session_state, "agent_plan", None):
        try:
            from .planner import AgentPlan

            ap_ro = AgentPlan.from_dict(session_state.agent_plan)
        except Exception:
            ap_ro = None
    if ap_ro is None and plan is not None and getattr(plan, "router_intent", None):
        ap_ro = plan

    if ap_ro is not None and str(getattr(ap_ro, "router_intent", "") or "") == "read_only_analysis":
        thinking = str(getattr(ap_ro, "exploration_thinking", "") or "").strip()
        if thinking:
            lines.extend(["", "**Explorer thinking:**", thinking[:1200]])
        try:
            from .read_only_explorer import _source_digests

            digest_rows = _source_digests(session_state, ap_ro, limit=10) if session_state else []
            if digest_rows:
                lines.extend(["", "**Tier digests:**"])
                for row in digest_rows[:8]:
                    sid = str(row.get("source_id") or "")
                    digest = str(row.get("digest") or "").strip()
                    if sid and digest:
                        lines.append(f"- **{sid}**: {digest[:500]}")
            elif getattr(ap_ro, "source_digests", None):
                lines.extend(["", "**Tier digests (cached):**"])
                for sid, digest in list((ap_ro.source_digests or {}).items())[:8]:
                    if str(digest).strip():
                        lines.append(f"- **{sid}**: {str(digest).strip()[:500]}")
        except Exception:
            pass

    if plan is not None:
        try:
            from .plan_state import build_evidence_answer, build_final_answer_from_plan, _is_read_only_coverage_plan

            if not _is_read_only_coverage_plan(plan):
                ans = build_evidence_answer(plan, query) or build_final_answer_from_plan(plan, query)
                if ans and ans.strip():
                    lines.extend(["", ans.strip()])
        except Exception:
            pass

    collected = _evidence_collected(session_state, plan)
    if collected:
        lines.extend(["", "**확보된 evidence:**", ", ".join(collected[:10])])

    goal = query.strip() or ""
    if goal:
        lines.extend(["", f"**요청:** {goal[:400]}"])

    if len("\n".join(lines).strip()) < 120:
        lines.extend([
            "",
            "source registry에 등록된 source_id만 사용해 ReadSource로 재시도해 주세요.",
        ])
    return "\n".join(lines).strip()


def build_recovery_prose(
    query: str,
    *,
    phase: str | None,
    plan: Any | None = None,
    session_state: Any | None = None,
    reason: str = "",
) -> str:
    ap_ro: Any | None = None
    if session_state is not None and getattr(session_state, "agent_plan", None):
        try:
            from .planner import AgentPlan

            ap_ro = AgentPlan.from_dict(session_state.agent_plan)
        except Exception:
            ap_ro = None
    router_intent = str(getattr(ap_ro, "router_intent", "") or getattr(plan, "router_intent", "") or "")
    if router_intent == "read_only_analysis":
        partial = build_partial_final_prose(
            query,
            plan=ap_ro or plan,
            session_state=session_state,
            reason=reason or "empty_outgoing",
        )
        # #region agent log
        try:
            import json
            import time

            with open("/home/yunahe/.cursor/debug-694f50.log", "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "sessionId": "694f50",
                            "location": "response_guard.py:build_recovery_prose",
                            "message": "read_only_recovery_prose",
                            "data": {"reason": reason, "chars": len(partial or ""), "phase": phase or ""},
                            "hypothesisId": "C",
                            "runId": "pre-fix",
                            "timestamp": int(time.time() * 1000),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass
        # #endregion
        if partial and len(partial.strip()) > 80:
            return partial
    if is_prose_phase(phase) or reason in ("bad_ping_pong", "next_action_answer", "xml_parse_failure"):
        return build_partial_final_prose(
            query,
            plan=plan,
            session_state=session_state,
            reason=reason or ("bad_ping_pong" if phase == "partial_final_answer" else ""),
        )
    return (
        "로컬 LLM이 tool_planning 단계에서 유효한 tool_call 또는 prose를 생성하지 못했습니다. "
        "같은 질문을 새 채팅에서 다시 시도하거나, Read/Grep으로 필요한 파일을 직접 확인해 주세요."
    )


def apply_nonempty_guard(
    response: dict[str, Any],
    *,
    phase: str | None = None,
    intent_name: str = "",
    query: str = "",
    plan: Any | None = None,
    session_state: Any | None = None,
    reason: str = "",
) -> tuple[dict[str, Any], bool]:
    """Ensure Cursor never receives tool_calls=0 and content=''."""
    if not is_empty_outgoing(response):
        return response, False

    text = build_recovery_prose(
        query,
        phase=phase,
        plan=plan,
        session_state=session_state,
        reason=reason,
    )
    try:
        msg = response["choices"][0]["message"]
        choice = response["choices"][0]
    except (KeyError, IndexError, TypeError):
        response = {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "object": "chat.completion",
        }
        LOG.warning(
            "response_guard applied reason=%s phase=%s intent=%s (rebuilt choices)",
            reason or "empty_outgoing",
            phase or "",
            intent_name,
        )
        return response, True

    msg["content"] = text
    msg.pop("tool_calls", None)
    choice["finish_reason"] = "stop"
    LOG.warning(
        "response_guard applied reason=%s phase=%s intent=%s chars=%d",
        reason or "empty_outgoing",
        phase or "",
        intent_name,
        len(text),
    )
    return response, True
