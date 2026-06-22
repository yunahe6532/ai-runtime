"""Agent Brain — LLM Planner shadow (Phase 2.1: observe only, no hot path change)."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx

from .planner_contract import VALID_ACTIONS, PlannerDecision
from .runtime_state import MAX_RUNTIME_STATE_PROMPT_CHARS, RuntimeState

LOG = logging.getLogger("agent_brain.llm_planner")

LLM_PLANNER_SHADOW_ENABLED = os.getenv("LLM_PLANNER_SHADOW_ENABLED", "0") == "1"
LLM_PLANNER_TIMEOUT_SEC = float(os.getenv("LLM_PLANNER_TIMEOUT_SEC", "15"))
LLM_PLANNER_MAX_TOKENS = int(os.getenv("LLM_PLANNER_MAX_TOKENS", "512"))
LLM_PLANNER_MODEL = os.getenv("LLM_PLANNER_MODEL", os.getenv("READ_ONLY_EXPLORER_MODEL", "fast"))
LLM_PLANNER_TEMPERATURE = float(os.getenv("LLM_PLANNER_TEMPERATURE", "0.1"))


def llm_planner_shadow_enabled() -> bool:
    return os.getenv("LLM_PLANNER_SHADOW_ENABLED", "0") == "1"

LLM_VALID_ACTIONS = frozenset({
    "read", "grep", "glob", "shell", "edit", "summarize", "final", "ask_user", "recover",
})

PLANNER_JSON_SCHEMA = (
    '{"action":"read|grep|glob|shell|edit|summarize|final|ask_user|recover",'
    '"target_files":["path"],'
    '"target_symbols":["symbol"],'
    '"reason":"short rationale",'
    '"evidence_needed":["tag"],'
    '"confidence":0.0,'
    '"stop_condition":"optional",'
    '"risk_flags":["flag"]}'
)

PLANNER_SYSTEM_PROMPT = f"""You are a Local LLM Runtime planner in shadow mode.
Given RuntimeState JSON, choose the next planner action.

Rules:
- Output ONLY one JSON object matching PlannerDecision. No markdown, prose, XML, or tool_call blocks.
- Do NOT output chain-of-thought or a thinking field. Put rationale in reason only.
- Use only these actions: {", ".join(sorted(LLM_VALID_ACTIONS))}
- Pick targets from working_set, journal, anchors, or project index when possible.
- confidence is 0.0 to 1.0.

Schema: {PLANNER_JSON_SCHEMA}"""


def _fallback_decision(
    reason: str,
    *,
    action: str = "recover",
    risk_flags: list[str] | None = None,
    confidence: float = 0.0,
) -> PlannerDecision:
    act = action if action in LLM_VALID_ACTIONS else "recover"
    return PlannerDecision(
        action=act,
        reason=reason[:500],
        confidence=confidence,
        risk_flags=list(risk_flags or []),
        stop_condition="shadow_fallback",
    )


def _extract_json_content(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _validate_llm_decision(decision: PlannerDecision | None) -> tuple[PlannerDecision | None, str]:
    if decision is None:
        return None, "parse_fail"
    if decision.action not in LLM_VALID_ACTIONS:
        return None, "invalid_action"
    if decision.action not in VALID_ACTIONS:
        return None, "invalid_action"
    return decision, ""


def _invoke_llm(prompt_json: str) -> tuple[str, dict[str, Any]]:
    """Call gateway; returns (content, meta). Meta always includes status."""
    meta: dict[str, Any] = {"status": "ok", "model": LLM_PLANNER_MODEL}
    body = {
        "model": LLM_PLANNER_MODEL,
        "stream": False,
        "temperature": LLM_PLANNER_TEMPERATURE,
        "max_tokens": LLM_PLANNER_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt_json},
        ],
    }
    timeout = httpx.Timeout(
        connect=5.0,
        read=LLM_PLANNER_TIMEOUT_SEC,
        write=10.0,
        pool=5.0,
    )
    try:
        from adapters.gateway import chat_completion

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        result = chat_completion(
            method="POST",
            path="/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            body_bytes=payload,
            body_json=body,
            backend_hint="fast",
            stream=False,
            timeout=timeout,
        )
        meta["status_code"] = getattr(result, "status_code", None)
        if getattr(result, "status_code", 500) != 200:
            meta["status"] = "http_error"
            return "", meta
        choices = (getattr(result, "json_data", None) or {}).get("choices") or []
        content = str((choices[0].get("message") or {}).get("content") or "")
        meta["content_chars"] = len(content)
        if (choices[0].get("message") or {}).get("tool_calls"):
            meta["status"] = "tool_call_forbidden"
            return content, meta
        return content, meta
    except httpx.TimeoutException:
        meta["status"] = "timeout"
        return "", meta
    except Exception as exc:
        meta["status"] = "error"
        meta["error"] = str(exc)[:200]
        LOG.warning("llm_planner invoke failed: %s", exc)
        return "", meta


def propose_llm_shadow_decision(
    runtime_state: RuntimeState,
    *,
    _invoke: Any | None = None,
) -> tuple[PlannerDecision, dict[str, Any]]:
    """LLM shadow planner — uses runtime_state.to_prompt_json() only."""
    meta: dict[str, Any] = {
        "enabled": True,
        "status": "ok",
        "prompt_chars": 0,
    }
    prompt_json = runtime_state.to_prompt_json()
    meta["prompt_chars"] = len(prompt_json)

    if len(prompt_json) > MAX_RUNTIME_STATE_PROMPT_CHARS:
        meta["status"] = "oversized_prompt"
        dec = _fallback_decision(
            "llm shadow: runtime_state prompt exceeds budget",
            risk_flags=["oversized_prompt"],
        )
        return dec, meta

    invoke = _invoke or _invoke_llm
    content, call_meta = invoke(prompt_json)
    meta.update(call_meta)

    status = meta.get("status", "ok")
    if status == "timeout":
        return _fallback_decision("llm shadow: timeout", risk_flags=["timeout"]), meta
    if status in ("http_error", "error", "tool_call_forbidden"):
        return _fallback_decision(
            f"llm shadow: {status}",
            action="recover",
            risk_flags=[status],
        ), meta

    raw = _extract_json_content(content)
    if not raw:
        meta["status"] = "parse_fail"
        return _fallback_decision(
            "llm shadow: JSON parse failed",
            action="recover",
            risk_flags=["parse_fail"],
        ), meta

    decision = PlannerDecision.from_dict(raw)
    validated, err = _validate_llm_decision(decision)
    if validated is None:
        meta["status"] = err or "invalid_action"
        return _fallback_decision(
            f"llm shadow: {err}",
            action="recover" if err != "parse_fail" else "ask_user",
            risk_flags=[err or "invalid_action"],
        ), meta

    meta["status"] = "ok"
    meta["action"] = validated.action
    return validated, meta
