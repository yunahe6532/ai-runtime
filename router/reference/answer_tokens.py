"""Mode-based max_tokens for final_answer — not a fixed short cap."""

from __future__ import annotations

import os
from typing import Any

ANSWER_MODE_TOKENS = {
    "normal": int(os.getenv("FINAL_TOKENS_NORMAL", "2500")),
    "analysis": int(os.getenv("FINAL_TOKENS_ANALYSIS", "4500")),
    "handoff": int(os.getenv("FINAL_TOKENS_HANDOFF", "6500")),
    "continue": int(os.getenv("FINAL_TOKENS_CONTINUE", "3500")),
}

_ANALYSIS_KW = (
    "분석",
    "문제점",
    "요약",
    "검증 결과",
    "어떤 구조",
    "상세분석",
    "로그 분석",
)
_CONTINUE_KW = (
    "cut off",
    "exceeded the output token",
    "continue from where you left off",
)
_HANDOFF_KW = ("handoff", "vision.md", "architecture.md", "문서", "정리해줘")


def detect_answer_mode(intent_name: str, query: str) -> str:
    ql = (query or "").lower()
    if intent_name == "continue_previous" or any(k in ql for k in _CONTINUE_KW):
        return "continue"
    if any(k in ql for k in _HANDOFF_KW):
        return "handoff"
    if intent_name in ("explain", "log_analysis", "project_inspection", "debug"):
        return "analysis"
    if any(k in query for k in _ANALYSIS_KW):
        return "analysis"
    return "normal"


def apply_final_answer_tokens(
    body: dict[str, Any],
    intent_name: str,
    query: str,
) -> None:
    mode = detect_answer_mode(intent_name, query)
    target = ANSWER_MODE_TOKENS.get(mode, ANSWER_MODE_TOKENS["normal"])
    existing = body.get("max_tokens")
    if not isinstance(existing, int) or existing < target:
        body["max_tokens"] = target
    body.setdefault("_answer_mode", mode)
