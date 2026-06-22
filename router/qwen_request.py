"""Qwen3.6 request options: thinking preservation, sampling, phase policy."""

from __future__ import annotations

import os
from typing import Any

PRESERVE_THINKING = os.getenv("PRESERVE_THINKING", "1") == "1"
ENABLE_THINKING_TOOL_PLANNING = os.getenv("ENABLE_THINKING_TOOL_PLANNING", "1") == "1"
ENABLE_THINKING_FINAL_ANSWER = os.getenv("ENABLE_THINKING_FINAL_ANSWER", "0") == "1"
PRESERVE_THINKING_TOOL_LOOP = os.getenv("PRESERVE_THINKING_TOOL_LOOP", "0") == "1"

AGENT_TEMPERATURE = float(os.getenv("AGENT_TEMPERATURE", "0.6"))
AGENT_TOP_P = float(os.getenv("AGENT_TOP_P", "0.95"))
AGENT_TOP_K = int(os.getenv("AGENT_TOP_K", "20"))

AGENT_INTENTS = frozenset(
    {"shell_task", "benchmark", "log_analysis", "code_edit", "agent", "debug"}
)


def _is_pure_tool_loop(body: dict[str, Any]) -> bool:
    """Heuristic: last N assistant turns are tool_calls only (Read/Shell repeat)."""
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return False
    tail = messages[-8:]
    tool_turns = 0
    for msg in reversed(tail):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        tcs = msg.get("tool_calls") or []
        if not tcs:
            break
        names = []
        for tc in tcs:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                names.append(str(fn.get("name") or ""))
        if names and all(n in ("Read", "Grep", "Glob") for n in names):
            tool_turns += 1
        else:
            break
    return tool_turns >= 2


def apply_qwen_request_opts(
    body: dict[str, Any],
    intent_name: str = "",
    phase: str = "",
    plan_phase: str = "",
) -> None:
    """Set chat_template_kwargs + coding sampling per agent phase."""
    kwargs = body.get("chat_template_kwargs")
    if not isinstance(kwargs, dict):
        kwargs = {}

    agent_like = intent_name in AGENT_INTENTS or phase in ("tool_planning", "final_answer")

    if agent_like and phase == "tool_planning":
        kwargs["enable_thinking"] = ENABLE_THINKING_TOOL_PLANNING
        pure_loop = _is_pure_tool_loop(body) or plan_phase == "validation_required"
        if PRESERVE_THINKING and (PRESERVE_THINKING_TOOL_LOOP or not pure_loop):
            kwargs["preserve_thinking"] = True
        else:
            kwargs.pop("preserve_thinking", None)
    elif agent_like and phase == "final_answer":
        kwargs["enable_thinking"] = ENABLE_THINKING_FINAL_ANSWER
        kwargs["preserve_thinking"] = False
    elif intent_name in ("explain", "casual"):
        kwargs["enable_thinking"] = False
        kwargs.pop("preserve_thinking", None)
    elif not body.get("tools") and intent_name not in AGENT_INTENTS:
        kwargs["enable_thinking"] = False

    if kwargs:
        body["chat_template_kwargs"] = kwargs
    elif "chat_template_kwargs" in body:
        body.pop("chat_template_kwargs", None)

    if agent_like:
        body["temperature"] = AGENT_TEMPERATURE
        body["top_p"] = AGENT_TOP_P
        # llama-server accepts top_k in body on recent builds
        body["top_k"] = AGENT_TOP_K
