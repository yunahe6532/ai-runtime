"""LLM Evidence Judge — static pre-eval + batch LLM sufficiency + guard validation.

Judge is a verifier, not a tool executor:
  explore batch (planner/agent proposes tools → runtime executes)
  → evidence summaries accumulated
  → static pre-eval
  → LLM judge (batch unit, not per-tool)
  → guarded decision → phase
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .evidence_store import evidence_items_for_judge
from .loop_guard import is_bad_ping_pong, should_block_final_answer
from adapters.memory import SessionState
from .planner import AgentPlan, DEFAULT_EVIDENCE, can_final_answer

LOG = logging.getLogger("router.evidence_judge")

LONG_URL = os.getenv("LONG_URL", "http://llama-long:8082").rstrip("/")
EVIDENCE_JUDGE_ENABLED = os.getenv("EVIDENCE_JUDGE_ENABLED", "1") == "1"
EVIDENCE_JUDGE_MODE = os.getenv("EVIDENCE_JUDGE_MODE", "hybrid")  # static | llm | hybrid
EVIDENCE_JUDGE_MIN_COVERAGE = float(os.getenv("EVIDENCE_JUDGE_MIN_COVERAGE", "0.5"))
EVIDENCE_JUDGE_BATCH_SIZE = int(os.getenv("EVIDENCE_JUDGE_BATCH_SIZE", "2"))
MAX_REMAINING_TOOL_TURNS = int(os.getenv("MAX_REMAINING_TOOL_TURNS", "6"))
MAX_JUDGE_NEXT_ACTIONS = int(os.getenv("MAX_JUDGE_NEXT_ACTIONS", "3"))
JUDGE_ROUND_LIMIT = int(os.getenv("JUDGE_ROUND_LIMIT", "2"))
SAME_FILE_RETRY_LIMIT = int(os.getenv("SAME_FILE_RETRY_LIMIT", "1"))
JUDGE_LLM_MAX_TOKENS = int(os.getenv("JUDGE_LLM_MAX_TOKENS", "512"))

JudgeDecisionType = Literal[
    "continue_explore",
    "replan",
    "final_ready",
    "clarify",
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]

INTENT_EXTRA_EVIDENCE: dict[str, list[str]] = {
    "runtime_diagnosis": ["fix_strategy_seen"],
    "log_analysis": ["error_pattern_seen"],
    "benchmark_analysis": ["bottleneck_seen"],
}


@dataclass
class StaticEval:
    coverage: float = 0.0
    new_evidence: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    collected_summaries: list[dict[str, str]] = field(default_factory=list)
    visited_paths: list[str] = field(default_factory=list)
    repeated_actions: list[str] = field(default_factory=list)
    turns_since_progress: int = 0
    tool_call_turns: int = 0
    token_budget_left: int = 0
    repetition_risk: str = "low"
    novelty: str = "medium"
    minimal_static_met: bool = False
    bad_ping_pong: bool = False
    explore_batch_ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JudgeDecision:
    """Judge output — sufficiency verdict + suggested next explore actions (not executed by judge)."""

    sufficient: bool = False
    allow_final: bool = False
    confidence: float = 0.5
    reason: str = ""
    missing_evidence: list[str] = field(default_factory=list)
    next_actions: list[dict[str, Any]] = field(default_factory=list)
    repair_needed: bool = False
    # Internal phase mapping
    decision: JudgeDecisionType = "continue_explore"
    sufficient_for_final: bool = False
    next_action: dict[str, Any] = field(default_factory=dict)
    required_before_final: list[str] = field(default_factory=list)
    stop_condition: str = ""
    source: str = "static"  # static | llm | guard_override | batch_wait

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _evidence_key(tag: str) -> str:
    return str(tag).split(":", 1)[0].strip()


def _summarize_evidence(tag: str) -> str:
    key = _evidence_key(tag)
    if "phase_distribution" in key or "loop_pattern" in key:
        return tag.split(":", 1)[-1] if ":" in tag else tag
    return tag[:120]


def _coverage(needed: list[str], collected: list[str]) -> float:
    if not needed:
        return 1.0 if collected else 0.0
    done = 0
    for n in needed:
        key = _evidence_key(n)
        if any(key in _evidence_key(c) or str(n).lower() in str(c).lower() for c in collected):
            done += 1
    return done / max(1, len(needed))


def _missing_evidence(needed: list[str], collected: list[str]) -> list[str]:
    missing: list[str] = []
    for n in needed:
        key = _evidence_key(n)
        if any(key in _evidence_key(c) or str(n).lower() in str(c).lower() for c in collected):
            continue
        missing.append(n)
    return missing


def effective_evidence_needed(plan: AgentPlan, query: str) -> list[str]:
    needed = list(plan.evidence_needed or [])
    extra = INTENT_EXTRA_EVIDENCE.get(plan.task_intent, [])
    for e in extra:
        if e not in needed:
            needed.append(e)
    defaults = DEFAULT_EVIDENCE.get(plan.task_intent, [])
    for e in defaults:
        if e not in needed:
            needed.append(e)
    q = (query or "").lower()
    if any(k in q for k in ("튜닝", "코드", "fix", "수정", "plan_state", "agent_exec")):
        for e in ("code_location_seen", "fix_strategy_seen"):
            if e not in needed:
                needed.append(e)
    return list(dict.fromkeys(needed))


def _visited_paths(state: SessionState) -> list[str]:
    paths: list[str] = []
    for p in state.files_read or []:
        try:
            paths.append(str(Path(p).as_posix()))
        except Exception:
            paths.append(str(p))
    return paths[-20:]


def _suggested_next_actions(
    plan: AgentPlan,
    query: str,
    static: StaticEval,
) -> list[dict[str, Any]]:
    """Hints for planner/agent — judge does NOT execute these."""
    candidates: list[dict[str, Any]] = []
    visited = set(static.visited_paths)
    missing = static.missing_evidence

    if any("code_location" in m or "fix_strategy" in m for m in missing):
        for q in ("should_block_final_answer", "last_judge_decision", "AgentPhase.FINAL"):
            candidates.append({"tool": "Grep", "query": q, "reason": f"locate guard/phase logic ({q})"})

    code_targets = [
        ("router/plan_state.py", "phase transition / final gate logic"),
        ("router/agent_exec.py", "XML leak handling / postprocess"),
        ("router/loop_guard.py", "ping-pong / progress guard"),
        ("router/evidence_judge.py", "judge batch + guard validation"),
    ]
    for rel, reason in code_targets:
        full = str(PROJECT_ROOT / rel)
        if full in visited or rel in visited:
            continue
        if any(k in " ".join(missing).lower() for k in ("code", "fix", "loop", "phase", "xml", "judge")):
            candidates.append({"tool": "Read", "target": full, "reason": reason})

    flows = sorted((PROJECT_ROOT / "tmp" / "cursor-captures").glob("*.flow.json"))
    if flows and any("flow" in m or "loop" in m or "phase" in m for m in missing):
        flow_path = str(flows[-1])
        candidates.append({
            "tool": "Grep",
            "target": flow_path,
            "query": "phase|pack_tokens|saved_pct",
            "reason": "Grep latest flow trace fields (not full Read)",
        })

    na = plan.next_action or {}
    if na.get("tool") and na.get("tool") not in ("answer", "final"):
        target = str(na.get("target") or na.get("path") or "")
        query_s = str(na.get("query") or "")
        key = target or query_s
        if key and key not in visited:
            candidates.insert(0, {
                "tool": na.get("tool"),
                "target": target,
                "query": query_s,
                "reason": str(na.get("reason") or "planner next_action"),
            })

    filtered: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    for c in candidates:
        tool = str(c.get("tool") or "")
        target = str(c.get("target") or c.get("path") or "")
        if tool == "Read" and target:
            try:
                from .read_guard import check_read_allowed

                allowed, _, _ = check_read_allowed(target, {})
                if not allowed:
                    continue
            except ImportError:
                pass
        key = f"{tool}:{target}"
        if key in seen_targets:
            continue
        seen_targets.add(key)
        filtered.append(c)
        if len(filtered) >= MAX_JUDGE_NEXT_ACTIONS:
            break

    return filtered[:MAX_JUDGE_NEXT_ACTIONS]


def should_run_judge_batch(
    state: SessionState,
    static: StaticEval,
    plan: AgentPlan,
) -> bool:
    """Run full judge after explore batch, not after every single tool."""
    if static.bad_ping_pong:
        return True
    if int(getattr(state, "tools_since_judge", 0) or 0) >= EVIDENCE_JUDGE_BATCH_SIZE:
        return True
    if static.minimal_static_met and can_final_answer(plan):
        return True
    if static.tool_call_turns >= MAX_REMAINING_TOOL_TURNS:
        return True
    if static.coverage >= 1.0 and static.missing_evidence == []:
        return True
    return False


def evaluate_exploration_static(
    state: SessionState,
    plan: AgentPlan,
    *,
    query: str = "",
    tool_call_turns: int = 0,
    pack_tokens: int = 0,
    pack_budget: int = 12000,
) -> StaticEval:
    needed = effective_evidence_needed(plan, query)
    collected = list(plan.evidence_collected or [])
    missing = _missing_evidence(needed, collected)
    cov = _coverage(needed, collected)
    visited = _visited_paths(state)

    repeated: list[str] = []
    if int(getattr(state, "same_action_repeated", 0) or 0) >= 1:
        sig = str(getattr(state, "last_action_sig", "") or "")
        if sig:
            repeated.append(sig)

    novelty = "high" if int(getattr(state, "steps_since_evidence", 0) or 0) == 0 else "low"
    if cov >= 0.8 and missing:
        novelty = "medium"

    rep_risk = "low"
    if repeated:
        rep_risk = "high"
    elif int(getattr(state, "turns_since_progress", 0) or 0) >= 2:
        rep_risk = "medium"

    item_summaries = evidence_items_for_judge(state)
    if item_summaries:
        summaries = item_summaries
    else:
        summaries = [
            {"type": _evidence_key(c), "summary": _summarize_evidence(c)}
            for c in collected[:12]
        ]

    minimal = cov >= EVIDENCE_JUDGE_MIN_COVERAGE and bool(collected)
    if plan.task_intent == "runtime_diagnosis":
        minimal = minimal and any("code_location" in _evidence_key(c) for c in collected)

    static = StaticEval(
        coverage=cov,
        new_evidence=[c for c in collected[-3:]],
        missing_evidence=missing,
        collected_summaries=summaries,
        visited_paths=visited,
        repeated_actions=repeated,
        turns_since_progress=int(getattr(state, "turns_since_progress", 0) or 0),
        tool_call_turns=tool_call_turns,
        token_budget_left=max(0, pack_budget - pack_tokens),
        repetition_risk=rep_risk,
        novelty=novelty,
        minimal_static_met=minimal,
        bad_ping_pong=is_bad_ping_pong(state),
    )
    static.explore_batch_ready = should_run_judge_batch(state, static, plan)
    return static


def build_judge_input(
    state: SessionState,
    plan: AgentPlan,
    static: StaticEval,
    query: str,
) -> dict[str, Any]:
    needed = effective_evidence_needed(plan, query)
    return {
        "user_goal": (query or plan.goal or "")[:400],
        "current_plan": {
            "intent": plan.task_intent,
            "confidence": plan.confidence,
            "evidence_needed": needed,
        },
        "collected_evidence": static.collected_summaries,
        "evidence_items": evidence_items_for_judge(state),
        "missing_evidence": static.missing_evidence,
        "visited_paths": static.visited_paths[-10:],
        "suggested_next_actions": _suggested_next_actions(plan, query, static),
        "static_signals": {
            "coverage": round(static.coverage, 3),
            "novelty": static.novelty,
            "repetition_risk": static.repetition_risk,
            "turns_since_progress": static.turns_since_progress,
            "tool_call_turns": static.tool_call_turns,
            "tools_since_judge": int(getattr(state, "tools_since_judge", 0) or 0),
            "explore_round": int(getattr(state, "explore_round", 0) or 0),
            "token_budget_left": static.token_budget_left,
            "bad_ping_pong": static.bad_ping_pong,
        },
        "constraints": {
            "judge_does_not_execute_tools": True,
            "avoid_repeated_paths": True,
            "max_remaining_tool_turns": max(0, MAX_REMAINING_TOOL_TURNS - static.tool_call_turns),
            "must_not_final_without_code_location": plan.task_intent == "runtime_diagnosis",
        },
    }


def _normalize_llm_decision(data: dict[str, Any]) -> JudgeDecision:
    sufficient = bool(data.get("sufficient") or data.get("sufficient_for_final"))
    allow_final = bool(data.get("allow_final"))
    repair = bool(data.get("repair_needed"))
    next_actions = list(data.get("next_actions") or [])
    if not next_actions and data.get("next_action"):
        next_actions = [dict(data.get("next_action") or {})]
    next_actions = next_actions[:MAX_JUDGE_NEXT_ACTIONS]

    if repair:
        decision: JudgeDecisionType = "replan"
    elif allow_final or sufficient:
        decision = "final_ready"
    elif data.get("decision") == "clarify":
        decision = "clarify"
    else:
        decision = "continue_explore"

    legacy = str(data.get("decision") or "")
    if legacy in ("continue_explore", "replan", "final_ready", "clarify"):
        decision = legacy  # type: ignore[assignment]

    sufficient_for_final = allow_final or sufficient or decision == "final_ready"
    next_action = next_actions[0] if next_actions else {}

    return JudgeDecision(
        sufficient=sufficient,
        allow_final=allow_final,
        confidence=float(data.get("confidence") or 0.5),
        reason=str(data.get("reason") or "")[:500],
        missing_evidence=list(data.get("missing_evidence") or data.get("required_before_final") or []),
        next_actions=next_actions,
        repair_needed=repair,
        decision=decision,
        sufficient_for_final=sufficient_for_final,
        next_action=next_action,
        required_before_final=list(data.get("required_before_final") or data.get("missing_evidence") or []),
        stop_condition=str(data.get("stop_condition") or "")[:300],
        source="llm",
    )


def _static_decision(plan: AgentPlan, static: StaticEval, query: str) -> JudgeDecision:
    suggestions = _suggested_next_actions(plan, query, static)

    if static.bad_ping_pong:
        return JudgeDecision(
            decision="replan",
            repair_needed=True,
            confidence=0.9,
            sufficient=False,
            allow_final=False,
            sufficient_for_final=False,
            reason="bad ping-pong detected — replan required",
            next_actions=suggestions,
            source="static",
        )

    if static.missing_evidence and static.coverage < EVIDENCE_JUDGE_MIN_COVERAGE:
        na = suggestions[0] if suggestions else {"tool": "Read", "reason": "gather missing evidence"}
        return JudgeDecision(
            decision="continue_explore",
            confidence=0.7,
            sufficient=False,
            allow_final=False,
            sufficient_for_final=False,
            reason=f"missing evidence: {', '.join(static.missing_evidence[:4])}",
            missing_evidence=static.missing_evidence,
            next_actions=suggestions or [na],
            next_action=na,
            required_before_final=static.missing_evidence,
            source="static",
        )

    if can_final_answer(plan) and static.minimal_static_met:
        return JudgeDecision(
            decision="final_ready",
            sufficient=True,
            allow_final=True,
            confidence=0.75,
            sufficient_for_final=True,
            reason="static coverage and minimal requirements met",
            next_actions=[{"tool": "answer", "reason": "evidence sufficient"}],
            next_action={"tool": "answer", "reason": "evidence sufficient"},
            source="static",
        )

    na = suggestions[0] if suggestions else {}
    return JudgeDecision(
        decision="continue_explore",
        confidence=0.6,
        sufficient=False,
        allow_final=False,
        sufficient_for_final=False,
        reason="insufficient coverage for confident final",
        missing_evidence=static.missing_evidence,
        next_actions=suggestions,
        next_action=na,
        required_before_final=static.missing_evidence,
        source="static",
    )


def _batch_wait_decision(static: StaticEval) -> JudgeDecision:
    return JudgeDecision(
        decision="continue_explore",
        sufficient=False,
        allow_final=False,
        sufficient_for_final=False,
        confidence=0.5,
        reason=f"explore batch in progress ({static.tool_call_turns} tools, wait for batch judge)",
        missing_evidence=static.missing_evidence,
        source="batch_wait",
    )


def llm_evidence_judge(judge_input: dict[str, Any]) -> JudgeDecision | None:
    try:
        import httpx
    except ImportError:
        return None

    schema = (
        "Return ONLY JSON: sufficient (bool), confidence (0-1), reason (string), "
        "missing_evidence (string[]), next_actions ([{tool, query?, path?/target?, reason}]), "
        "allow_final (bool), repair_needed (bool). "
        "You evaluate evidence only — do NOT claim tools were executed."
    )
    payload = {
        "model": "model.gguf",
        "stream": False,
        "temperature": 0.1,
        "max_tokens": JUDGE_LLM_MAX_TOKENS,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an evidence sufficiency judge for a coding agent runtime. "
                    "Given collected evidence summaries, decide if the user question can be answered. "
                    "Suggest next_actions for the explore agent — you do not execute tools. "
                    "Set allow_final=true only when code locations and fix strategy are confirmed for diagnosis. "
                    + schema
                ),
            },
            {
                "role": "user",
                "content": json.dumps(judge_input, ensure_ascii=False),
            },
        ],
    }
    try:
        r = httpx.post(f"{LONG_URL}/v1/chat/completions", json=payload, timeout=45.0)
        r.raise_for_status()
        content = str(r.json()["choices"][0]["message"].get("content") or "")
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(content[start : end + 1])
        return _normalize_llm_decision(data)
    except Exception as exc:
        LOG.warning("llm_evidence_judge failed: %s", exc)
        return None


def validate_decision(
    decision: JudgeDecision,
    static: StaticEval,
    state: SessionState,
    plan: AgentPlan,
) -> JudgeDecision:
    """Guard judge verdict — block unsafe final, repeated paths, no-progress loops."""
    out = JudgeDecision(**decision.to_dict())
    visited = set(static.visited_paths)

    for na in out.next_actions:
        target = str(na.get("target") or na.get("path") or "")
        tool = str(na.get("tool") or "")
        if tool == "Read" and target and target in visited:
            out.decision = "continue_explore"
            out.allow_final = False
            out.sufficient = False
            out.sufficient_for_final = False
            out.reason = f"guard: repeated path {Path(target).name}"
            out.source = "guard_override"
            alts = _suggested_next_actions(plan, plan.goal, static)
            out.next_actions = [a for a in alts if str(a.get("target") or "") not in visited][:4]
            out.next_action = out.next_actions[0] if out.next_actions else {}
            break

    if static.turns_since_progress >= 3 and out.decision == "continue_explore":
        if static.minimal_static_met:
            out.decision = "final_ready"
            out.allow_final = True
            out.sufficient = True
            out.sufficient_for_final = True
            out.reason = "guard: no progress 3 turns — force final with available evidence"
            out.source = "guard_override"
        else:
            out.decision = "replan"
            out.repair_needed = True
            out.reason = "guard: no progress loop — replan"
            out.source = "guard_override"

    if out.decision == "final_ready" or out.allow_final:
        if not static.minimal_static_met and plan.task_intent in ("runtime_diagnosis", "log_analysis"):
            out.decision = "continue_explore"
            out.allow_final = False
            out.sufficient = False
            out.sufficient_for_final = False
            out.reason = "guard: minimal static requirements not met for diagnosis"
            out.source = "guard_override"
        blocked, reason = should_block_final_answer(
            state,
            can_final=True,
            task_intent=plan.task_intent,
            intent_name="agent",
        )
        if blocked:
            out.decision = "continue_explore"
            out.allow_final = False
            out.sufficient = False
            out.sufficient_for_final = False
            out.reason = f"guard: {reason}"
            out.source = "guard_override"

    if static.tool_call_turns >= MAX_REMAINING_TOOL_TURNS and out.decision == "continue_explore":
        if static.coverage >= 0.4:
            out.decision = "final_ready"
            out.allow_final = True
            out.sufficient = True
            out.sufficient_for_final = True
            out.reason = "guard: tool turn budget exhausted — best-effort final"
            out.source = "guard_override"

    return out


def apply_decision_to_plan(plan: AgentPlan, decision: JudgeDecision) -> AgentPlan:
    """Store judge hints on plan — planner/agent executes next tool, not judge."""
    if decision.next_actions:
        na = decision.next_actions[0]
    else:
        na = decision.next_action or {}
    tool = str(na.get("tool") or "")
    if tool and tool not in ("answer", "final"):
        plan.next_action = {
            "tool": tool,
            "target": str(na.get("target") or na.get("path") or ""),
            "query": str(na.get("query") or ""),
            "reason": str(na.get("reason") or decision.reason)[:200],
        }
    elif decision.decision == "final_ready" and decision.allow_final:
        plan.next_action = {
            "tool": "answer",
            "target": "",
            "reason": decision.reason or "judge: allow_final",
        }
    req = decision.required_before_final or decision.missing_evidence
    for e in req:
        if e not in plan.evidence_needed:
            plan.evidence_needed.append(e)
    return plan


def evaluate_exploration(
    state: SessionState,
    plan: AgentPlan,
    *,
    query: str = "",
    tool_call_turns: int = 0,
    pack_tokens: int = 0,
    intent_name: str = "",
) -> tuple[StaticEval, JudgeDecision]:
    static = evaluate_exploration_static(
        state,
        plan,
        query=query,
        tool_call_turns=tool_call_turns,
        pack_tokens=pack_tokens,
    )

    state.required_evidence_types = effective_evidence_needed(plan, query)
    state.missing_evidence = list(static.missing_evidence)
    state.last_static_eval = static.to_dict()

    if int(getattr(state, "judge_round", 0) or 0) >= JUDGE_ROUND_LIMIT:
        decision = _static_decision(plan, static, query)
        decision.next_actions = decision.next_actions[:MAX_JUDGE_NEXT_ACTIONS]
        decision.source = "round_limit"
        state.last_judge_decision = decision.to_dict()
        LOG.info("evidence_judge round_limit reached (%d)", JUDGE_ROUND_LIMIT)
        return static, decision

    if not should_run_judge_batch(state, static, plan):
        decision = _batch_wait_decision(static)
        state.last_judge_decision = decision.to_dict()
        LOG.info(
            "evidence_judge batch_wait tools_since=%d coverage=%.2f",
            int(getattr(state, "tools_since_judge", 0) or 0),
            static.coverage,
        )
        return static, decision

    decision = _static_decision(plan, static, query)

    use_llm = EVIDENCE_JUDGE_ENABLED and EVIDENCE_JUDGE_MODE in ("llm", "hybrid")
    if use_llm and EVIDENCE_JUDGE_MODE == "hybrid":
        use_llm = (
            static.coverage >= EVIDENCE_JUDGE_MIN_COVERAGE
            or plan.task_intent in ("runtime_diagnosis", "log_analysis", "benchmark_analysis")
            or static.bad_ping_pong
        )

    if use_llm:
        judge_input = build_judge_input(state, plan, static, query)
        llm_dec = llm_evidence_judge(judge_input)
        if llm_dec is not None:
            decision = llm_dec

    decision = validate_decision(decision, static, state, plan)
    apply_decision_to_plan(plan, decision)

    state.judge_round = int(getattr(state, "judge_round", 0) or 0) + 1
    state.tools_since_judge = 0
    state.last_judge_decision = decision.to_dict()
    state.judge_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        from adapters.observe import current_run_id, emit_task

        rid = current_run_id()
        if rid:
            emit_task(
                rid,
                "evidence.judge",
                f"{decision.decision} allow_final={decision.allow_final} conf={decision.confidence:.2f} cov={static.coverage:.2f}",
            )
    except ImportError:
        pass

    LOG.info(
        "evidence_judge decision=%s source=%s coverage=%.2f allow_final=%s reason=%s",
        decision.decision,
        decision.source,
        static.coverage,
        decision.allow_final,
        (decision.reason or "")[:120],
    )
    return static, decision


def phase_from_decision(decision: JudgeDecision) -> str:
    if decision.source == "batch_wait":
        return "tool_planning"
    if decision.decision == "final_ready" and (decision.allow_final or decision.sufficient_for_final):
        return "final_answer"
    return "tool_planning"
