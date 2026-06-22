"""Router-side planner summary → Cursor chat thinking (reasoning_content).

Primary path (P1): `runtime_inspector.py` — `<details>` content mirror.
This module remains for legacy `reasoning_content` when CURSOR_RUNTIME_INSPECTOR=0.
"""

from __future__ import annotations

import os
from typing import Any

CURSOR_REASONING_ENABLED = os.getenv("CURSOR_REASONING_ENABLED", "1") == "1"
CURSOR_REASONING_FIELD = os.getenv("CURSOR_REASONING_FIELD", "reasoning_content")


def cursor_reasoning_enabled() -> bool:
    return CURSOR_REASONING_ENABLED


def build_cursor_reasoning(
    *,
    agent_plan: dict[str, Any] | None = None,
    query: str = "",
    phase: str = "tool_planning",
    plan_phase_hint: str = "",
) -> str:
    """Planner/run state → short Korean summary for Cursor thinking UI."""
    ap = agent_plan or {}
    lines: list[str] = []

    goal = str(ap.get("goal") or query or "").strip()
    intent = str(ap.get("task_intent") or "general")
    if goal:
        lines.append(f"요청: {goal[:240]}")
    lines.append(f"단계: {phase}")
    lines.append(f"의도: {intent}")

    na = ap.get("next_action") or {}
    tool = str(na.get("tool") or "").strip()
    if tool and tool != "answer":
        target = str(na.get("target") or "").strip()
        reason = str(na.get("reason") or "").strip()
        detail = f"{tool} {target}".strip()
        if reason:
            detail = f"{detail} — {reason}" if detail else reason
        lines.append(f"다음: {detail}")
    elif phase == "tool_planning":
        lines.append("파일·디렉터리 확인이 필요하면 Read / Glob / Shell을 사용합니다.")

    needed = list(ap.get("evidence_needed") or [])
    collected = list(ap.get("evidence_collected") or [])
    if needed:
        lines.append(f"필요 evidence: {', '.join(str(x) for x in needed[:5])}")
        if collected:
            lines.append(f"수집됨: {', '.join(str(x) for x in collected[:5])}")
        elif needed == ["target_coverage"] or str(ap.get("router_intent") or "") == "read_only_analysis":
            hits = list(ap.get("source_hits") or [])
            candidates = list(ap.get("source_candidates") or [])
            if hits:
                lines.append(f"source_hits ({len(hits)}/{len(candidates)}): {', '.join(hits[:6])}")
            reg_raw = ap.get("source_registry") or {}
            missing: list[str] = []
            if reg_raw and candidates:
                try:
                    from reference.source_registry import SourceRegistry

                    reg = SourceRegistry.from_dict(reg_raw)
                    hit_set = set(hits)
                    cand_set = set(candidates)
                    missing = [
                        s.id for s in reg.sources
                        if s.exists and s.id in cand_set and s.id not in hit_set
                    ]
                except (TypeError, ValueError, KeyError):
                    missing = [c for c in candidates if c not in hits]
            elif candidates:
                missing = [c for c in candidates if c not in hits]
            if missing:
                lines.append(f"아직 필요: {', '.join(missing[:6])}")
                lines.append("같은 source_id 반복 금지 — missing만 GlobSource/ReadSource")
            else:
                lines.append("source coverage 충족 → final answer 대기")
        else:
            lines.append("evidence 미충족 — final answer 보류")
    elif phase == "final_answer" and collected:
        lines.append(f"evidence 충족: {', '.join(str(x) for x in collected[:5])}")

    thinking = str(ap.get("exploration_thinking") or "").strip()
    if phase == "final_answer" and thinking:
        lines.append(f"탐색 reasoning: {thinking[:900]}")
    digests = ap.get("source_digests") if isinstance(ap.get("source_digests"), dict) else {}
    if phase == "final_answer" and digests:
        tier_names = ", ".join(list(digests.keys())[:6])
        lines.append(f"tier digest 수집: {tier_names}")

    avoid = list(ap.get("avoid_actions") or [])
    if avoid:
        lines.append("회피: " + "; ".join(str(a) for a in avoid[:4]))

    hint = (plan_phase_hint or "").strip()
    if hint and hint not in (phase, ""):
        lines.append(f"plan phase: {hint}")

    return "\n".join(lines).strip()


def inject_cursor_reasoning(
    response: dict[str, Any],
    reasoning: str,
    *,
    field: str | None = None,
) -> bool:
    """Set reasoning on assistant message for Cursor chat UI."""
    text = (reasoning or "").strip()
    if not text:
        return False
    try:
        msg = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return False
    key = field or CURSOR_REASONING_FIELD
    msg[key] = text
    return True


def reasoning_from_state(state: Any | None, *, query: str = "", phase: str = "tool_planning") -> str:
    if state is None:
        return build_cursor_reasoning(query=query, phase=phase)
    return build_cursor_reasoning(
        agent_plan=getattr(state, "agent_plan", None),
        query=query or str(getattr(state, "current_query", "") or ""),
        phase=phase,
        plan_phase_hint=str(getattr(state, "phase_hint", "") or ""),
    )
