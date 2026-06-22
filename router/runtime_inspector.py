"""Cursor chat Runtime Inspector — `<details>` content mirror for debugging."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CURSOR_RUNTIME_INSPECTOR = os.getenv("CURSOR_RUNTIME_INSPECTOR", os.getenv("RUNTIME_INSPECTOR_ENABLED", "1")) == "1"
CURSOR_RUNTIME_INSPECTOR_MODE = os.getenv(
    "CURSOR_RUNTIME_INSPECTOR_MODE",
    os.getenv("CURSOR_THINKING_DISPLAY", "content"),
)  # content | content_details | reasoning | both
RUNTIME_INSPECTOR_COMPACT = os.getenv("RUNTIME_INSPECTOR_COMPACT", "1") == "1"

EVIDENCE_LABELS: dict[str, str] = {
    "project_tree_seen": "Project Tree",
    "core_files_seen": "Core Files",
    "readme_seen": "README",
    "architecture_seen": "Architecture",
    "planner_seen": "Planner",
    "memory_seen": "Memory Store",
    "compose_port": "Compose Port",
    "agent_benchmark": "Agent Benchmark",
    "runtime_score": "Runtime Score",
    "flow_phase_seen": "Flow Phase",
    "phase_distribution_seen": "Phase Distribution",
    "loop_pattern_seen": "Loop Pattern",
    "bottleneck_seen": "Bottleneck",
    "code_location_seen": "Code Location",
    "artifact_seen": "Artifact",
    "xml_leak_seen": "XML Leak",
    "tools_stripped_seen": "Tools Stripped",
    "runtime_score_seen": "Runtime Score",
    "agent_benchmark_seen": "Agent Benchmark",
}

PHASE_ORDER = (
    "planning",
    "tool_planning",
    "searching",
    "reading",
    "evidence",
    "final_answer",
)

CORE_RUNTIME_KNOWN: list[tuple[str, str]] = [
    ("Planner", "planner.py"),
    ("Executor", "agent_exec.py"),
    ("Memory", "legacy/memory_store.py"),
    ("Agent Runs", "legacy/agent_runs.py"),
    ("Prompt Builder", "prompt_builder.py"),
    ("Evidence", "evidence_extractors.py"),
    ("Plan Guard", "plan_state.py"),
    ("Progress UI", "ui/"),
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def runtime_inspector_enabled() -> bool:
    return CURSOR_RUNTIME_INSPECTOR


def _fmt_ms(sec: float | None) -> str:
    if sec is None or sec < 0:
        return "n/a"
    if sec < 1:
        return f"{sec * 1000:.0f} ms"
    return f"{sec:.2f} s"


def _progress_bar(pct: int, width: int = 6) -> str:
    pct = max(0, min(100, pct))
    filled = round(width * pct / 100)
    return "[" + "■" * filled + "□" * (width - filled) + f"] {pct}%"


def _evidence_key(tag: str) -> str:
    base = str(tag).split(":", 1)[0].strip()
    return base


def _evidence_label(tag: str) -> str:
    key = _evidence_key(tag)
    if key in EVIDENCE_LABELS:
        return EVIDENCE_LABELS[key]
    return key.replace("_", " ").title()


def _phase_progress(phase: str, evidence_needed: list[str], evidence_collected: list[str]) -> int:
    phase = (phase or "tool_planning").lower()
    if phase == "final_answer":
        return 100
    base = {
        "planning": 15,
        "tool_planning": 35,
        "searching": 50,
        "reading": 65,
        "evidence": 80,
    }.get(phase, 30)
    if evidence_needed:
        done = sum(1 for n in evidence_needed if _evidence_item_satisfied(n, evidence_collected))
        ev_pct = int(100 * done / max(1, len(evidence_needed)))
        return min(95, base + ev_pct // 5)
    return base


def _dashboard_bars(phase: str) -> str:
    weights = {
        "planning": (90, 20, 10, 5, 0),
        "tool_planning": (100, 40, 25, 10, 0),
        "searching": (100, 80, 30, 15, 0),
        "reading": (100, 60, 90, 40, 0),
        "evidence": (100, 70, 80, 85, 20),
        "final_answer": (100, 100, 100, 100, 100),
    }
    p = (phase or "tool_planning").lower()
    w = weights.get(p, weights["tool_planning"])
    labels = ("Planning", "Searching", "Reading", "Analyzing", "Final")
    lines = ["Runtime", ""]
    for label, val in zip(labels, w):
        bar_w = 8
        filled = round(bar_w * val / 100)
        lines.append(f"{label:<12}{'█' * filled}{'░' * (bar_w - filled)}")
    return "\n".join(lines)


@dataclass
class RuntimeInspectorContext:
    run_id: str = ""
    phase: str = "tool_planning"
    query: str = ""
    intent: str = ""
    backend: str = ""
    agent_plan: dict[str, Any] = field(default_factory=dict)
    session_state: Any | None = None
    raw_tokens: int = 0
    pack_tokens: int = 0
    saved_pct: float = 0.0
    cursor_message_count: int = 0
    proxy_message_count: int = 0
    tools_stripped: bool = False
    llm_elapsed_sec: float | None = None
    total_elapsed_sec: float | None = None
    completion_tokens: int = 0
    prompt_tokens: int = 0
    turn_index: int = 0
    gpu_vram_gb: float | None = None
    dram_gb: float | None = None
    timeline_steps: list[str] = field(default_factory=list)
    replay_steps: list[str] = field(default_factory=list)
    judge_decision: dict[str, Any] = field(default_factory=dict)
    static_eval: dict[str, Any] = field(default_factory=dict)
    runtime_turn: dict[str, Any] = field(default_factory=dict)


def _plan_steps(ap: dict[str, Any], phase: str) -> list[str]:
    steps: list[str] = []
    intent = str(ap.get("task_intent") or "general")
    if intent in ("project_inspection", "repo_summary"):
        steps = [
            "Read project tree",
            "Read core runtime files",
            "Collect evidence",
            "Generate summary",
        ]
    elif intent == "benchmark_analysis":
        steps = ["Locate benchmark artifacts", "Read results", "Summarize metrics"]
    else:
        done = list(ap.get("done_when") or [])
        if done:
            steps = [str(x) for x in done[:6]]
        else:
            na = ap.get("next_action") or {}
            tool = str(na.get("tool") or "").strip()
            if tool and tool != "answer":
                steps.append(f"{tool} {na.get('target', '')}".strip())
            if phase != "final_answer":
                steps.append("Collect evidence")
            steps.append("Generate answer")
    return steps


def _evidence_item_satisfied(needed: str, collected: list[str]) -> bool:
    key = _evidence_key(needed)
    if key == "target_coverage":
        return any(str(c).startswith("source_hit:") for c in collected)
    collected_keys = {_evidence_key(c) for c in collected}
    return key in collected_keys or any(str(c).startswith(str(needed)) for c in collected)


def _build_evidence_section(needed: list[str], collected: list[str]) -> list[str]:
    lines: list[str] = ["Evidence", ""]
    if not needed:
        if collected:
            for c in collected[:8]:
                lines.append(f"✔ {_evidence_label(c)}")
        else:
            lines.append("(none required)")
        return lines

    for n in needed:
        hit = _evidence_item_satisfied(n, collected)
        mark = "✔" if hit else "□"
        lines.append(f"{mark} {_evidence_label(n)}")
    done = sum(1 for n in needed if _evidence_item_satisfied(n, collected))
    lines.append("")
    lines.append(f"Progress: {done} / {len(needed)}")
    return lines


def _build_timeline(ctx: RuntimeInspectorContext, ap: dict[str, Any]) -> list[str]:
    if ctx.timeline_steps:
        lines = ["Timeline", ""]
        for i, step in enumerate(ctx.timeline_steps):
            prefix = "✓" if i < len(ctx.timeline_steps) - 1 or ctx.phase == "final_answer" else "⏳"
            lines.append(f"{prefix} {step}")
        return lines

    lines = ["Timeline", ""]
    lines.append("✓ Planning")
    state = ctx.session_state
    files = list(ap.get("known_files") or [])
    if state is not None:
        files = list(getattr(state, "files_read", None) or files)

    if files:
        for f in files[:6]:
            name = Path(str(f)).name
            lines.append(f"✓ Read {name}")
        if len(files) > 6:
            lines.append(f"✓ … +{len(files) - 6} more")

    collected = list(ap.get("evidence_collected") or [])
    needed = list(ap.get("evidence_needed") or [])
    if needed:
        done = sum(
            1
            for n in needed
            if any(_evidence_key(c) == _evidence_key(n) for c in collected)
        )
        if done >= len(needed):
            lines.append("✓ Evidence complete")
        else:
            lines.append("⏳ Waiting for evidence")

    if ctx.phase == "final_answer":
        lines.append("✓ Final answer")
    elif ap.get("next_action", {}).get("tool"):
        na = ap["next_action"]
        lines.append(f"→ Next: {na.get('tool')} {na.get('target', '')}".strip())

    return lines


def _build_memory_hierarchy_section(ctx: RuntimeInspectorContext) -> list[str]:
    mh = {}
    if ctx.session_state is not None:
        mh = dict(getattr(ctx.session_state, "last_memory_hierarchy", None) or {})
    if not mh and ctx.runtime_turn:
        mh = dict(ctx.runtime_turn.get("memory_hierarchy") or {})
    if not mh:
        return []

    lines = ["Memory Hierarchy", ""]
    funnel = [
        ("raw_history", mh.get("raw_history_tokens", 0)),
        ("stored_memory", mh.get("stored_memory_tokens", 0)),
        ("retrieved", mh.get("retrieved_memory_tokens", 0)),
        ("prompt_pack", mh.get("prompt_pack_tokens", 0)),
        ("gpu_context", mh.get("gpu_context_tokens", 0)),
    ]
    max_tok = max(int(v or 0) for _, v in funnel) or 1
    for stage, tok in funnel:
        bar_len = max(1, int(20 * int(tok or 0) / max_tok))
        lines.append(f"  {stage:<14} {int(tok or 0):>7,}  {'█' * bar_len}")
    lines.append("")
    lines.append(f"  compression_ratio: {float(mh.get('compression_ratio', 0) or 0):.3f}")
    lines.append(f"  memory_hit_rate: {float(mh.get('memory_hit_rate', 0) or 0):.2f}")
    lines.append(f"  re-read avoidance: {float(mh.get('repeated_read_avoidance', 0) or 0):.2f}")
    lines.append(f"  coverage_score: {float(mh.get('coverage_score', 0) or 0):.2f}")
    if mh.get("stored_memory_items") is not None:
        lines.append(f"  stored_items: {int(mh.get('stored_memory_items', 0))}")
    return lines


def _build_memory_snapshot(ap: dict[str, Any], state: Any | None) -> list[str]:
    lines = ["Memory", ""]
    project = ""
    if state is not None:
        project = str(getattr(state, "project_key", "") or getattr(state, "workspace_path", "") or "")
    if not project:
        project = "cursor-local-llm"
    lines.append(f"Project: {project}")
    lines.append("")

    known_files = set(str(p) for p in (ap.get("known_files") or []))
    if state is not None:
        for p in getattr(state, "files_read", None) or []:
            known_files.add(str(p))

    lines.append("Known")
    for label, needle in CORE_RUNTIME_KNOWN:
        hit = any(needle in p for p in known_files) or needle.replace("/", "") in " ".join(known_files)
        if not hit and state is not None:
            artifacts = getattr(state, "artifacts", None) or []
            hit = any(needle in str(a) for a in artifacts)
        mark = "✔" if hit else "·"
        lines.append(f"  {mark} {label}")

    lines.append("")
    lines.append("Session")
    if state is not None:
        lines.append(f"  Requests: {getattr(state, 'total_requests', 0)}")
        lines.append(f"  Artifacts: {len(getattr(state, 'artifacts', None) or [])}")
        lines.append(f"  Files read: {len(getattr(state, 'files_read', None) or [])}")
        journal = list(getattr(state, "task_journal", None) or [])
        anchors = list(getattr(state, "evidence_anchors", None) or [])
        handoff = dict(getattr(state, "handoff", None) or {})
        lines.append(f"  Journal events: {len(journal)}")
        lines.append(f"  Evidence anchors: {len(anchors)}")
        if handoff.get("updated_at"):
            lines.append(f"  Handoff: {handoff.get('updated_at')}")
        rt = dict(getattr(state, "last_runtime_turn", None) or {})
        if "final_report_used" in rt:
            lines.append(
                f"  Final report: used={rt.get('final_report_used')} "
                f"chars={rt.get('final_report_chars', 0)}"
            )
        for j in journal[-3:]:
            if isinstance(j, dict):
                lines.append(
                    f"    · [{j.get('kind', '?')}] {str(j.get('target', ''))[:60]}"
                )
        for a in anchors[-3:]:
            if isinstance(a, dict):
                loc = str(a.get("path") or "")
                ls = a.get("line_start")
                if ls:
                    loc += f":L{ls}"
                lines.append(f"    · anchor {loc[:70]}")
    return lines


def _build_planner_runtime_section(state: Any | None) -> list[str]:
    if state is None:
        return []
    prs = dict(getattr(state, "planner_runtime_state", None) or {})
    shadow = dict(getattr(state, "last_planner_shadow", None) or {})
    if not prs and not shadow:
        return []

    lines = ["Planner RuntimeState", ""]
    if prs:
        lines.append(f"  phase: {prs.get('phase', '?')}")
        lines.append(f"  router_intent: {prs.get('router_intent', '')}")
        lines.append(f"  journal_tail: {len(prs.get('task_journal_tail') or [])}")
        lines.append(f"  anchors: {len(prs.get('evidence_anchor_summary') or [])}")
        prompt = str(getattr(state, "planner_runtime_state_prompt", "") or "")
        if prompt:
            lines.append(f"  prompt_chars: {len(prompt)}")
        ws = prs.get("working_set_summary") or {}
        targets = list(ws.get("priority_targets") or [])[:4]
        if targets:
            lines.append("  ws_targets: " + ", ".join(str(t) for t in targets))

    if shadow:
        cmp_ = dict(shadow.get("comparison") or {})
        triple = dict(shadow.get("triple_comparison") or getattr(state, "last_planner_llm_shadow", None) or {})
        if isinstance(triple, dict) and triple.get("triple_comparison"):
            triple = dict(triple.get("triple_comparison") or triple)
        lines.append("")
        lines.append("Shadow Decision (rule vs heuristic)")
        lines.append(f"  match: {shadow.get('match', cmp_.get('match'))}")
        lines.append(
            f"  rule: {cmp_.get('rule_action', '?')} → heuristic: {cmp_.get('shadow_action', '?')}"
        )
        mismatch = shadow.get("mismatch_reason") or cmp_.get("mismatch_reason") or ""
        if mismatch:
            lines.append(f"  mismatch: {mismatch}")
        llm_dec = shadow.get("llm_shadow_decision") or {}
        llm_meta = shadow.get("llm_shadow_meta") or {}
        if llm_dec or triple:
            lines.append("")
            lines.append("Triple Compare (rule / heuristic / LLM)")
            lines.append(
                f"  rule={triple.get('rule_action', cmp_.get('rule_action', '?'))} "
                f"heuristic={triple.get('heuristic_action', cmp_.get('shadow_action', '?'))} "
                f"llm={triple.get('llm_action', llm_dec.get('action', 'n/a'))}"
            )
            if llm_meta.get("status"):
                lines.append(f"  llm_status: {llm_meta.get('status')}")
            if triple.get("action_match_rule_llm") is not None:
                lines.append(f"  action_match(rule↔llm): {triple.get('action_match_rule_llm')}")
            if triple.get("target_overlap_rule_llm") is not None:
                lines.append(f"  target_overlap(rule↔llm): {triple.get('target_overlap_rule_llm')}")
        if shadow.get("would_change_hot_path") or triple.get("would_change_hot_path"):
            lines.append("  would_change_hot_path: true")
        if shadow.get("target_overlap") is not None:
            lines.append(f"  target_overlap(rule↔heuristic): {shadow.get('target_overlap')}")
    trace_path = ""
    try:
        from explorer_trace import default_trace_path

        trace_path = str(default_trace_path())
    except ImportError:
        pass
    if trace_path:
        lines.append(f"  trace: {trace_path}")
    return lines


def _build_budget_section(ctx: RuntimeInspectorContext) -> list[str]:
    rt = ctx.runtime_turn or {}
    budget = dict(rt.get("budget_plan") or {})
    need = dict(rt.get("context_need") or {})
    if not budget and not need:
        return []

    lines = ["Runtime Budget", ""]
    lines.append(f"- intent: {rt.get('intent') or need.get('intent', ctx.intent or 'n/a')}")
    lines.append(f"- phase: {rt.get('phase') or ctx.phase}")
    if rt.get("dynamic_budget_enabled") is not None:
        lines.append(f"- dynamic: {rt.get('dynamic_budget_enabled')}")
    if budget:
        lines.append(f"- mode: {budget.get('mode', 'n/a')}")
        for key in (
            "retrieved", "session_tail", "artifact", "current_task",
            "state", "plan", "system", "delta", "output_reserved",
        ):
            val = budget.get(key)
            if val is not None:
                lines.append(f"- {key}: {int(val):,} tokens")
    if rt.get("retrieval_total_tokens"):
        lines.append(f"- retrieval_measured: {int(rt['retrieval_total_tokens']):,} tokens")

    items = rt.get("retrieval_items") or []
    if items:
        lines.append("")
        lines.append("RetrievalPack")
        for item in items[:5]:
            src = item.get("source", "?")
            tok = item.get("tokens", 0)
            sc = item.get("score", 0)
            lines.append(f"  - {src} ({tok} tok, score={sc:.2f})")
        missing = rt.get("retrieval_missing_targets") or []
        if missing:
            lines.append(f"  missing: {', '.join(str(m) for m in missing[:4])}")

    if need.get("required_sources"):
        lines.append("")
        lines.append(f"required_sources: {', '.join(need['required_sources'][:6])}")
    if need.get("coverage_targets"):
        lines.append(f"coverage_targets: {', '.join(need['coverage_targets'][:6])}")
    return lines


def _build_coverage_section(ctx: RuntimeInspectorContext) -> list[str]:
    rt = ctx.runtime_turn or {}
    if not rt and not ctx.static_eval:
        return []

    lines = ["Coverage", ""]
    score = rt.get("coverage_score")
    if score is not None:
        lines.append(f"- score: {float(score):.2f}")
    complete = rt.get("coverage_complete")
    if complete is not None:
        lines.append(f"- complete: {str(bool(complete)).lower()}")
    missing = rt.get("coverage_missing") or []
    if missing:
        lines.append(f"- missing: {', '.join(str(m) for m in missing[:5])}")
    action = rt.get("coverage_action")
    if action:
        lines.append(f"- action: {action}")

    truncated = rt.get("coverage_truncated") or []
    if truncated:
        lines.append("- truncated:")
        for t in truncated[:3]:
            src = t.get("source", "?")
            lost = t.get("lost_tokens", 0)
            lines.append(f"  - {src} (lost {lost} tok)")

    if rt.get("critical_source_truncated"):
        lines.append("- critical_source_truncated: true")
    if rt.get("latest_tool_result_missing"):
        lines.append("- latest_tool_result_missing: true")

    lines.append("")
    lines.append("Recovery / Final Gate")
    if rt.get("recovery_triggered") is not None:
        lines.append(f"- recovery_triggered: {str(bool(rt.get('recovery_triggered'))).lower()}")
    if rt.get("recovery_recovered") is not None:
        lines.append(f"- recovery_recovered: {str(bool(rt.get('recovery_recovered'))).lower()}")
    if rt.get("recovery_rounds"):
        lines.append(f"- recovery_rounds: {rt.get('recovery_rounds')}")
    reason = rt.get("final_blocked_reason")
    if reason:
        lines.append(f"- final_blocked: {reason}")
    elif complete:
        lines.append("- final_blocked: false")
    return lines


def _build_context_need_section(ctx: RuntimeInspectorContext) -> list[str]:
    need = dict((ctx.runtime_turn or {}).get("context_need") or {})
    if not need and ctx.agent_plan.get("context_need"):
        need = dict(ctx.agent_plan.get("context_need") or {})
    if not need:
        return []
    lines = ["ContextNeed", ""]
    lines.append(f"intent: {need.get('intent', 'n/a')}")
    if need.get("must_include"):
        lines.append("must_include:")
        for m in need["must_include"][:6]:
            lines.append(f"  - {m}")
    if need.get("priority"):
        lines.append("priority:")
        for k, v in list(need["priority"].items())[:6]:
            lines.append(f"  - {k}: {v}")
    return lines


def _build_context_section(ctx: RuntimeInspectorContext) -> list[str]:
    lines = ["Context", ""]
    lines.append(f"Cursor history: {ctx.cursor_message_count} messages")
    lines.append(f"Proxy pack: {ctx.proxy_message_count} messages")
    if ctx.raw_tokens > 0:
        lines.append(f"Raw est.: {ctx.raw_tokens:,} tokens")
    if ctx.pack_tokens > 0:
        lines.append(f"Prompt est.: {ctx.pack_tokens:,} tokens")
    if ctx.saved_pct > 0:
        lines.append(f"Compressed: {ctx.saved_pct:.1f}%")
    if ctx.prompt_tokens > 0:
        lines.append(f"LLM prompt: {ctx.prompt_tokens:,} tokens")
    if ctx.completion_tokens > 0:
        lines.append(f"LLM completion: {ctx.completion_tokens:,} tokens")
    dropped = max(0, ctx.cursor_message_count - ctx.proxy_message_count)
    if ctx.cursor_message_count > 0:
        lines.append(f"Injected (proxy): {ctx.proxy_message_count}")
        lines.append(f"Dropped (delta): {dropped}")
    if ctx.tools_stripped:
        lines.append("Tools: stripped for final_answer")
    return lines


def _build_telemetry(ctx: RuntimeInspectorContext) -> list[str]:
    lines = ["Telemetry", ""]
    lines.append(f"LLM generation: {_fmt_ms(ctx.llm_elapsed_sec)}")
    if ctx.total_elapsed_sec is not None:
        lines.append(f"Request total: {_fmt_ms(ctx.total_elapsed_sec)}")
    if ctx.saved_pct > 0:
        lines.append(f"Compression: {ctx.saved_pct:.1f}%")
    if ctx.pack_tokens > 0:
        lines.append(f"Prompt est.: {ctx.pack_tokens:,} tokens")
    if ctx.completion_tokens > 0:
        lines.append(f"Completion: {ctx.completion_tokens:,} tokens")
    if ctx.gpu_vram_gb is not None:
        lines.append(f"GPU VRAM: {ctx.gpu_vram_gb:.1f} GB")
    else:
        lines.append("GPU VRAM: n/a (P3 scheduler)")
    if ctx.dram_gb is not None:
        lines.append(f"DRAM: {ctx.dram_gb:.1f} GB")
    else:
        lines.append("DRAM: n/a")
    lines.append(f"Backend: {ctx.backend or 'n/a'}")
    return lines


def _details_block(summary: str, body: str) -> str:
    body = (body or "").strip()
    if not body:
        return ""
    return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>"


def _build_judge_section(judge: dict[str, Any], static: dict[str, Any]) -> list[str]:
    if not judge and not static:
        return []
    lines = ["Evidence Judge", ""]
    if static:
        cov = static.get("coverage")
        if cov is not None:
            lines.append(f"Coverage: {float(cov) * 100:.0f}%")
        if static.get("novelty"):
            lines.append(f"Novelty: {static.get('novelty')}")
        if static.get("repetition_risk"):
            lines.append(f"Repetition risk: {static.get('repetition_risk')}")
        missing = static.get("missing_evidence") or []
        if missing:
            lines.append("Missing: " + ", ".join(str(m) for m in missing[:5]))
    if judge:
        allow = judge.get("allow_final", judge.get("sufficient_for_final"))
        lines.append(f"Decision: **{judge.get('decision', '?')}** (allow_final={allow})")
        lines.append(f"Judge confidence: {float(judge.get('confidence', 0)):.2f}")
        if judge.get("reason"):
            lines.append(f"Reason: {judge.get('reason')}")
        actions = judge.get("next_actions") or []
        if actions:
            lines.append("Suggested next:")
            for a in actions[:3]:
                tool = a.get("tool", "")
                tgt = a.get("target") or a.get("path") or a.get("query") or ""
                lines.append(f"  - {tool} {tgt}".strip())
        else:
            na = judge.get("next_action") or {}
            if na.get("tool"):
                lines.append(f"Next: {na.get('tool')} {na.get('target', na.get('query', ''))}".strip())
        if judge.get("source"):
            lines.append(f"Source: {judge.get('source')}")
    return lines


def build_runtime_inspector(ctx: RuntimeInspectorContext) -> str:
    """Build nested `<details>` markdown for Cursor chat content mirror."""
    ap = ctx.agent_plan or {}
    phase = ctx.phase or "tool_planning"
    intent = str(ap.get("task_intent") or ctx.intent or "general")
    confidence = float(ap.get("confidence") or 0.0)
    goal = str(ap.get("goal") or ctx.query or "")[:200]
    needed = list(ap.get("evidence_needed") or [])
    collected = list(ap.get("evidence_collected") or [])
    pct = _phase_progress(phase, needed, collected)

    snapshot_lines = [
        f"Run: `{ctx.run_id or 'n/a'}`",
        f"Phase: **{phase}**",
        f"Intent: {intent}",
    ]
    if confidence > 0:
        snapshot_lines.append(f"Confidence: {confidence:.2f}")
        if confidence < float(os.getenv("PLANNER_MIN_CONFIDENCE", "0.6")):
            snapshot_lines.append("⚠ Need clarification (low confidence)")
    if goal:
        snapshot_lines.append(f"Goal: {goal}")
    snapshot_lines.append("")
    snapshot_lines.append(f"Progress: {_progress_bar(pct)}")
    snapshot_lines.append("")
    snapshot_lines.extend(_build_evidence_section(needed, collected))
    snapshot_lines.append("")
    steps = _plan_steps(ap, phase)
    if steps:
        snapshot_lines.append("Plan")
        for i, s in enumerate(steps, 1):
            snapshot_lines.append(f"{i}. {s}")

    na = ap.get("next_action") or {}
    if na.get("tool") and str(na.get("tool")) != "answer":
        snapshot_lines.append("")
        snapshot_lines.append(f"Next: **{na.get('tool')}** {na.get('target', '')}".strip())
        if na.get("reason"):
            snapshot_lines.append(f"Reason: {na.get('reason')}")

    avoid = list(ap.get("avoid_actions") or [])
    if avoid:
        snapshot_lines.append("")
        snapshot_lines.append("Avoid: " + "; ".join(str(a) for a in avoid[:4]))

    sections = [
        _details_block("Runtime Snapshot", "\n".join(snapshot_lines)),
        _details_block("Runtime Budget", "\n".join(_build_budget_section(ctx))),
        _details_block("Coverage", "\n".join(_build_coverage_section(ctx))),
        _details_block("ContextNeed", "\n".join(_build_context_need_section(ctx))),
        _details_block("Evidence Judge", "\n".join(_build_judge_section(ctx.judge_decision, ctx.static_eval))),
        _details_block("Timeline", "\n".join(_build_timeline(ctx, ap))),
        _details_block("Telemetry", "\n".join(_build_telemetry(ctx))),
        _details_block("Context", "\n".join(_build_context_section(ctx))),
        _details_block("Memory Hierarchy", "\n".join(_build_memory_hierarchy_section(ctx))),
        _details_block("Memory", "\n".join(_build_memory_snapshot(ap, ctx.session_state))),
        _details_block("Planner RuntimeState", "\n".join(_build_planner_runtime_section(ctx.session_state))),
        _details_block("Dashboard", _dashboard_bars(phase)),
    ]

    if ctx.replay_steps:
        replay_body = "\n".join(
            [f"{i + 1}. {s}" for i, s in enumerate(ctx.replay_steps)]
        )
        sections.append(_details_block("Replay", replay_body))

    inner = "\n\n".join(s for s in sections if s)
    elapsed = _fmt_ms(ctx.llm_elapsed_sec) if ctx.llm_elapsed_sec else ""
    ev_done = sum(
        1
        for n in needed
        if any(str(n).split(":")[0] in str(c) for c in collected)
    )
    ev_total = len(needed) if needed else 0
    compact_bits = [
        f"Runtime · {phase}",
        f"evidence {ev_done}/{ev_total}" if ev_total else "",
        f"{ctx.saved_pct:.1f}% compressed" if ctx.saved_pct else "",
    ]
    rt = ctx.runtime_turn or {}
    if rt.get("coverage_score") is not None:
        compact_bits.append(f"cov {float(rt['coverage_score']):.2f}")
    if rt.get("final_blocked_reason"):
        compact_bits.append(f"blocked:{rt['final_blocked_reason']}")
    if RUNTIME_INSPECTOR_COMPACT:
        summary = " · ".join(b for b in compact_bits if b)
        if elapsed:
            summary = f"{summary} · {elapsed}"
    else:
        summary = f"Runtime Inspector ({elapsed})" if elapsed else "Runtime Inspector"
    return _details_block(summary, inner)


def build_inspector_from_state(
    state: Any | None,
    *,
    run_id: str = "",
    query: str = "",
    phase: str = "tool_planning",
    stats: Any | None = None,
    intent: str = "",
    backend: str = "",
    llm_elapsed_sec: float | None = None,
    total_elapsed_sec: float | None = None,
    cursor_message_count: int = 0,
    proxy_message_count: int = 0,
    usage: dict[str, Any] | None = None,
) -> str:
    ap = getattr(state, "agent_plan", None) or {} if state else {}
    ctx = RuntimeInspectorContext(
        run_id=run_id,
        phase=phase,
        query=query or str(getattr(state, "current_query", "") or ""),
        intent=intent,
        backend=backend,
        agent_plan=dict(ap) if isinstance(ap, dict) else {},
        session_state=state,
        turn_index=int(getattr(state, "turn_index", 0) or 0) if state else 0,
        llm_elapsed_sec=llm_elapsed_sec,
        total_elapsed_sec=total_elapsed_sec,
        cursor_message_count=cursor_message_count,
        proxy_message_count=proxy_message_count,
    )
    if stats is not None:
        ctx.raw_tokens = int(getattr(stats, "raw_tokens", 0) or 0)
        ctx.pack_tokens = int(getattr(stats, "pack_tokens", 0) or 0)
        ctx.saved_pct = float(getattr(stats, "saved_pct", 0.0) or 0.0)
        ctx.tools_stripped = bool(getattr(stats, "tools_stripped", False))
        if not ctx.backend:
            ctx.backend = str(getattr(stats, "backend", "") or "")
        if not ctx.intent:
            ctx.intent = str(getattr(stats, "intent", "") or "")

    if usage:
        ctx.prompt_tokens = int(usage.get("prompt_tokens") or 0)
        ctx.completion_tokens = int(usage.get("completion_tokens") or 0)

    ctx.timeline_steps = _timeline_from_state(state, ap if isinstance(ap, dict) else {})
    ctx.replay_steps = _replay_from_state(state, ap if isinstance(ap, dict) else {})
    if state is not None:
        ctx.judge_decision = dict(getattr(state, "last_judge_decision", None) or {})
        ctx.static_eval = dict(getattr(state, "last_static_eval", None) or {})
        ctx.runtime_turn = dict(getattr(state, "last_runtime_turn", None) or {})
    return build_runtime_inspector(ctx)


def _timeline_from_state(state: Any | None, ap: dict[str, Any]) -> list[str]:
    steps = ["Planning"]
    if state is None:
        return steps
    for cmd in (getattr(state, "commands_run", None) or [])[:3]:
        steps.append(f"Shell: {str(cmd)[:60]}")
    for f in (getattr(state, "files_read", None) or [])[:8]:
        steps.append(f"Read {Path(str(f)).name}")
    for ev in (ap.get("evidence_collected") or [])[:5]:
        steps.append(f"Evidence: {_evidence_label(str(ev))}")
    return steps


def _replay_from_state(state: Any | None, ap: dict[str, Any]) -> list[str]:
    if state is None:
        return []
    steps: list[str] = []
    step_n = int(ap.get("step_count") or 0)
    if step_n > 0:
        steps.append(f"Planner step {step_n}")
    for i, f in enumerate(getattr(state, "files_read", None) or [], 1):
        steps.append(f"Read {Path(str(f)).name}")
    if ap.get("evidence_collected"):
        steps.append("Evidence checkpoint")
    return steps


def inject_runtime_inspector(
    response: dict[str, Any],
    markdown: str,
    *,
    mode: str | None = None,
) -> bool:
    """Attach inspector markdown for SSE content mirror."""
    text = (markdown or "").strip()
    if not text:
        return False
    try:
        msg = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return False

    display_mode = (mode or CURSOR_RUNTIME_INSPECTOR_MODE).lower()
    if display_mode not in ("content", "both"):
        return True
    msg["_runtime_inspector"] = text
    return True


def inspector_content_from_message(msg: dict[str, Any]) -> str:
    """Extract inspector block for SSE (stripped from user-facing final prose)."""
    return str(msg.get("_runtime_inspector") or "").strip()
