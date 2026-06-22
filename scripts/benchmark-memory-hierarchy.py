#!/usr/bin/env python3
"""Memory Hierarchy benchmark — compression funnel + quality gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

os.environ.setdefault("GATEWAY_BACKEND", "mock")
os.environ.setdefault("MEMORY_STORE", "1")
os.environ.setdefault("DYNAMIC_BUDGET", "1")
os.environ.setdefault("COVERAGE_CHECK", "1")
os.environ.setdefault("VECTOR_RETRIEVAL", "0")
os.environ.setdefault("COVERAGE_THRESHOLD", "0.75")
os.environ.setdefault("RECOVERY_ENABLED", "0")

RAW_HISTORY_TOKENS = 80_000

QUALITY_GATES = {
    "raw_to_gpu_ratio_max": 0.05,
    "coverage_score_min": 0.8,
    "task_success_min": 0.95,
    "recovery_success_min": 0.95,
    "repeated_read_avoidance_min": 0.70,
}


@dataclass
class HierarchyCase:
    label: str
    raw_tokens: int = 0
    stored_items: int = 0
    stored_memory_tokens: int = 0
    retrieved_tokens: int = 0
    prompt_pack_tokens: int = 0
    gpu_context_tokens: int = 0
    ratio: float = 0.0
    coverage_score: float = 0.0
    task_success: bool = False
    recovery_count: int = 0
    recovery_success: bool = True
    repeated_read_avoidance: float = 1.0
    memory_hit_rate: float = 0.0
    coverage_fail_reasons: list[dict[str, Any]] = field(default_factory=list)
    missing_must_include: list[str] = field(default_factory=list)


def _seed_file_artifact(
    state: Any,
    req_id: str,
    delta_id: str,
    path: str,
    content: str,
    *,
    art_type: str = "file_read",
) -> Any:
    from dataclasses import asdict as dc_asdict

    from legacy.memory_store import ARTIFACT_DIR, Artifact

    artifact_id = f"{req_id}_{Path(path).name.replace('.', '_')}"
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = ARTIFACT_DIR / f"{artifact_id}.txt"
    raw_path.write_text(content, encoding="utf-8")
    art = Artifact(
        artifact_id=artifact_id,
        req_id=req_id,
        delta_id=delta_id,
        type=art_type,
        name=Path(path).name,
        path=path,
        raw_path=str(raw_path),
        chars=len(content),
        summary=content[:240],
        index_terms=[Path(path).name, Path(path).stem, *Path(path).stem.split("_")],
    )
    (ARTIFACT_DIR / f"{artifact_id}.json").write_text(
        json.dumps({k: getattr(art, k) for k in Artifact.__dataclass_fields__}, default=str),
        encoding="utf-8",
    )
    if artifact_id not in (state.artifacts or []):
        state.artifacts = list(state.artifacts or []) + [artifact_id]
    return art


def _seed_tool_artifact(
    state: Any,
    req_id: str,
    delta_id: str,
    content: str,
    *,
    name: str = "Grep",
) -> Any:
    return _seed_file_artifact(
        state,
        req_id,
        delta_id,
        "tool_result/grep.txt",
        content,
        art_type="tool_result",
    )


def _tool_message(content: str, name: str = "Grep") -> dict[str, Any]:
    return {"role": "tool", "name": name, "content": content}


def _base_body(query: str, *, label: str = "default", tool_msg: dict[str, Any] | None = None) -> dict[str, Any]:
    workspace = f"/bench/memory-qg-{label}"
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": f"Workspace Path: {workspace}\n<user_query>{query}</user_query>"},
        {"role": "assistant", "content": "I'll inspect the codebase."},
    ]
    if tool_msg:
        messages.append(tool_msg)
    messages.append({"role": "user", "content": query})
    return {"model": "test", "messages": messages, "max_tokens": 512}


def _apply_snap(row: HierarchyCase, snap: dict[str, Any], rt: dict[str, Any]) -> None:
    row.raw_tokens = int(snap.get("raw_history_tokens") or rt.get("raw_history_tokens") or RAW_HISTORY_TOKENS)
    row.stored_items = int(snap.get("stored_memory_items") or 0)
    row.stored_memory_tokens = int(snap.get("stored_memory_tokens") or 0)
    row.retrieved_tokens = int(snap.get("retrieved_memory_tokens") or rt.get("retrieval_total_tokens") or 0)
    row.prompt_pack_tokens = int(snap.get("prompt_pack_tokens") or 0)
    row.gpu_context_tokens = int(snap.get("gpu_context_tokens") or row.prompt_pack_tokens)
    row.ratio = float(snap.get("compression_ratio") or 0)
    if row.raw_tokens > 0 and row.gpu_context_tokens > 0:
        row.ratio = row.gpu_context_tokens / row.raw_tokens
    row.coverage_score = float(snap.get("coverage_score") or rt.get("coverage_score") or 0)
    row.repeated_read_avoidance = float(snap.get("repeated_read_avoidance") or 1.0)
    row.memory_hit_rate = float(snap.get("memory_hit_rate") or 0)
    row.recovery_count = int(rt.get("recovery_rounds") or 0)
    row.recovery_success = bool(rt.get("recovery_recovered", True))
    row.task_success = bool(rt.get("coverage_complete")) and row.coverage_score >= QUALITY_GATES["coverage_score_min"]


def _finalize_row(
    row: HierarchyCase,
    *,
    need: Any,
    retrieval_pack: Any,
    pack: Any,
    coverage: Any,
    expected_targets: list[str] | None = None,
) -> None:
    from coverage_checker import analyze_coverage_fail_reasons, check_must_include

    if coverage and not row.task_success:
        row.task_success = bool(getattr(coverage, "complete", False)) and row.coverage_score >= QUALITY_GATES["coverage_score_min"]
    if coverage and not row.task_success:
        text = ""
        if hasattr(pack, "body"):
            for msg in (pack.body or {}).get("messages", []):
                text += str(msg.get("content", ""))
        row.missing_must_include = check_must_include(need, text)
        row.coverage_fail_reasons = [
            d.to_dict() for d in analyze_coverage_fail_reasons(
                need, retrieval_pack, pack, coverage, expected_targets=expected_targets,
            )
        ]


def _refresh_context_need(state: Any, query: str, intent_name: str) -> None:
    """Bench isolation — rebuild need after setup (ingest may trigger replan path)."""
    from context_need import build_context_need
    from reference.planner import AgentPlan

    plan = AgentPlan.from_dict(state.agent_plan or {})
    need = build_context_need(plan, query, intent_name)
    merged = plan.to_dict()
    merged["context_need"] = need.to_dict()
    state.agent_plan = merged


def _run_turn(
    *,
    label: str,
    query: str,
    intent_name: str,
    setup: Any = None,
    enable_recovery: bool = False,
    expected_targets: list[str] | None = None,
) -> HierarchyCase:
    from adapters.gateway import chat_completion
    from adapters.memory import ingest_request
    from adapters.trace import clear_recorded_events, set_trace_context
    from coverage_checker import check_coverage
    from dynamic_context_scheduler import build_context_for_turn

    prev_recovery = os.environ.get("RECOVERY_ENABLED")
    if enable_recovery:
        os.environ["RECOVERY_ENABLED"] = "1"
        os.environ.setdefault("MAX_RECOVERY_ROUNDS", "2")
    else:
        os.environ["RECOVERY_ENABLED"] = "0"

    clear_recorded_events()
    body = _base_body(query, label=label)
    req_id = f"mem-qg-{label}"
    delta, state, arts = ingest_request(req_id, body, query=query)
    state.last_raw_tokens = RAW_HISTORY_TOKENS
    ap = dict(state.agent_plan or {})
    ap.pop("context_need", None)
    state.agent_plan = ap
    if setup:
        setup(state, delta, body, arts)
    _refresh_context_need(state, query, intent_name)

    set_trace_context(
        flow_id=req_id,
        run_id=req_id,
        backend="long",
        intent=intent_name,
        phase="tool_planning",
    )

    pack = build_context_for_turn(
        body=body,
        state=state,
        delta=delta,
        artifacts=arts,
        intent_name=intent_name,
        phase="tool_planning",
        backend="long",
        index=type("Idx", (), {"query": query})(),
        query=query,
    )

    chat_completion(
        method="POST",
        path="/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        body_bytes=json.dumps(body).encode(),
        body_json=body,
        backend_hint="long",
        stream=False,
    )

    snap = dict(getattr(state, "last_memory_hierarchy", None) or {})
    rt = dict(getattr(state, "last_runtime_turn", None) or {})
    row = HierarchyCase(label=label)
    _apply_snap(row, snap, rt)

    need = None
    retrieval_pack = None
    coverage = getattr(pack, "coverage", None)
    ap = state.agent_plan or {}
    if ap.get("context_need"):
        from context_need import ContextNeed

        need = ContextNeed.from_dict(ap["context_need"])
    if need and coverage:
        _finalize_row(row, need=need, retrieval_pack=retrieval_pack, pack=pack, coverage=coverage, expected_targets=expected_targets)
    elif coverage and not row.task_success:
        row.task_success = bool(getattr(coverage, "complete", False))

    if prev_recovery is None:
        os.environ.pop("RECOVERY_ENABLED", None)
    else:
        os.environ["RECOVERY_ENABLED"] = prev_recovery

    return row


def _setup_bugfix(state: Any, delta: Any, body: dict[str, Any], arts: list[Any]) -> None:
    from reference.planner import ensure_agent_plan

    content = '''
"""Dynamic budget allocation."""
def normalize_plan(plan, query):
    return plan

def allocate_dynamic(backend, phase, max_output, need, stats):
    """Core budget allocator — bugfix target."""
    return {"retrieved": 4096, "session_tail": 2048}

def build_with_budget(body, state, delta):
    return body
'''
    tool = "workspace_result\ncontext_budget.py:18:def allocate_dynamic\nExit code: 0"
    _seed_file_artifact(state, state.last_req_id or "mem", delta.delta_id, "router/context_budget.py", content)
    _seed_tool_artifact(state, state.last_req_id or "mem", delta.delta_id, tool)
    body["messages"].insert(-1, _tool_message(tool))
    plan = ensure_agent_plan(state, body["messages"][-1]["content"])
    plan.task_intent = "bugfix"
    plan.known_files = ["router/context_budget.py"]
    state.agent_plan = plan.to_dict()


def _setup_explore(state: Any, delta: Any, body: dict[str, Any], arts: list[Any]) -> None:
    from reference.planner import ensure_agent_plan

    policy = '''
"""Memory tier policy."""
class MemoryTier:
    SESSION = "session"
    GPU_HOT = "gpu_hot"

def build_working_set(prompt_sources, raw_history_tokens=0):
    return {"gpu_context_tokens": 2048}
'''
    hierarchy = '''
"""Funnel metrics raw → GPU."""
def compute_memory_hierarchy(**kwargs):
    return {"compression_ratio": 0.01}
'''
    tool = 'grep hit: memory_policy.py build_working_set\n{"role": "tool", "content": "artifact coverage OK"}'
    _seed_file_artifact(state, state.last_req_id or "mem", delta.delta_id, "router/runtime_core/memory_policy.py", policy)
    _seed_file_artifact(state, state.last_req_id or "mem", delta.delta_id, "router/runtime_core/memory_hierarchy.py", hierarchy)
    _seed_tool_artifact(state, state.last_req_id or "mem", delta.delta_id, tool)
    body["messages"].insert(-1, _tool_message(tool))
    plan = ensure_agent_plan(state, "explore router memory hierarchy design")
    plan.task_intent = "project_inspection"
    plan.known_files = [
        "router/runtime_core/memory_policy.py",
        "router/runtime_core/memory_hierarchy.py",
    ]
    state.agent_plan = plan.to_dict()
    state.read_counts = {"router/runtime_core/memory_policy.py": 1}


def _setup_recall(state: Any, delta: Any, body: dict[str, Any], arts: list[Any]) -> None:
    from reference.planner import ensure_agent_plan

    plan = ensure_agent_plan(state, "우리가 아까 memory hierarchy 결정 뭐였지?")
    plan.task_intent = "general"
    plan.goal = "previous decision: use 5-tier memory hierarchy with GPU working set cap 4K"
    plan.evidence_collected = ["session_state:turn-3", "previous decision recorded"]
    state.agent_plan = plan.to_dict()
    state.phase_state = {
        "session_context": "previous decision: prioritize working set over full history resend",
        "previous_decision": "5-tier memory hierarchy; API stateless, local LLM owns memory",
    }
    session_note = (
        "[session_state]\n"
        "previous decision: 5-tier memory hierarchy with GPU working set cap 4K\n"
        "agent plan: recall session memory hit\n"
    )
    _seed_file_artifact(state, state.last_req_id or "mem", delta.delta_id, "session/notes.md", session_note)


def _setup_doc_analysis(state: Any, delta: Any, body: dict[str, Any], arts: list[Any]) -> None:
    doc = '''
# Vision

## Memory Hierarchy

Local LLM owns memory tiers: session, artifact, vector, policy, gpu_hot.
Working set targets 2-4K tokens from 80K raw history.

### Summary
Compression without losing must_include targets.
'''
    _seed_file_artifact(state, state.last_req_id or "mem", delta.delta_id, "docs/VISION.md", doc)
    state.agent_plan = {
        "task_intent": "log_analysis",
        "goal": "analyze memory hierarchy section",
        "known_files": ["docs/VISION.md"],
        "evidence_collected": ["document:VISION.md"],
    }


def _setup_recovery(state: Any, delta: Any, body: dict[str, Any], arts: list[Any]) -> None:
    filler = "\n".join(f"# padding line {i}" for i in range(120))
    content = f"{filler}\n\ndef allocate_dynamic(backend, phase, max_output, need, stats):\n    return budget\n"
    tool = "partial grep — missing allocate_dynamic body initially\nExit code: 0"
    _seed_file_artifact(state, state.last_req_id or "mem", delta.delta_id, "router/context_budget.py", content)
    _seed_tool_artifact(state, state.last_req_id or "mem", delta.delta_id, tool)
    body["messages"].insert(-1, _tool_message(tool))
    state.agent_plan = {
        "task_intent": "html_validation",
        "goal": "fix allocate_dynamic after recovery",
        "known_files": ["router/context_budget.py"],
        "next_action": {"tool": "Read", "target": "router/context_budget.py", "reason": "recovery"},
    }
    os.environ["RECOVERY_BUDGET_BUMP"] = "2.5"


def _run_recovery_case() -> HierarchyCase:
    from context_budget import RetrievalStats, allocate_dynamic
    from context_need import ContextNeed
    from coverage_checker import analyze_coverage_fail_reasons, check_coverage
    from dynamic_context_scheduler import build_context_for_turn
    from prompt_builder import PromptPack, build_with_budget
    from recovery_scheduler import RecoveryScheduler
    from adapters.memory import RequestDelta, SessionState
    from adapters.retrieval import RetrievalItem, RetrievalPack, retrieve_for_need

    need = ContextNeed(
        intent="bugfix",
        required_sources=["retrieved_code", "tool_result"],
        must_include=["current user request", "active agent plan", "latest tool result"],
        coverage_targets=["context_budget.py", "context_budget.py::allocate_dynamic"],
    )
    query = "fix context_budget.py allocate_dynamic after truncation"
    body = _base_body(query, label="recovery", tool_msg=_tool_message("Exit code: 0\ngrep partial"))
    state = SessionState()
    state.last_req_id = "mem-qg-recovery"
    state.last_raw_tokens = RAW_HISTORY_TOKENS
    delta = RequestDelta(
        delta_id="d1",
        req_id="mem-qg-recovery",
        prev_req_id=None,
        prev_message_count=0,
        curr_message_count=4,
        added_count=4,
    )
    arts: list[Any] = []
    _setup_recovery(state, delta, body, arts)

    budget = allocate_dynamic("long", "tool_planning", 800, need, RetrievalStats())
    pack_fail = PromptPack(
        body={"messages": [{"role": "system", "content": "[Saved Agent Plan]\n[Task]\nfix"}]},
        phase="tool_planning",
        truncation_markers=[{"source": "context_budget.py", "critical": True, "lost_tokens": 900}],
    )
    cov_before = check_coverage(need, RetrievalPack(items=[]), pack_fail)
    retrieval = retrieve_for_need(state, query, delta, need, budget.retrieved)

    scheduler = RecoveryScheduler()
    result = scheduler.recover(
        context_need=need,
        budget=budget,
        retrieval_pack=retrieval,
        coverage=cov_before,
        retrieve_fn=lambda **kw: retrieve_for_need(state, query, delta, need, kw.get("budget_tokens", budget.retrieved)),
        build_fn=lambda **kw: build_with_budget(
            body=body,
            state=state,
            delta=delta,
            artifacts=arts,
            intent_name="bugfix",
            phase="tool_planning",
            backend="long",
            index=type("Idx", (), {"query": query})(),
            query=query,
            context_need=need,
            **{k: v for k, v in kw.items() if k in ("budget_plan", "retrieval_pack")},
        ),
        retrieve_kwargs={"state": state, "query": query, "delta": delta, "need": need},
        build_kwargs={
            "body": body,
            "state": state,
            "delta": delta,
            "artifacts": arts,
            "intent_name": "bugfix",
            "phase": "tool_planning",
            "backend": "long",
            "index": type("Idx", (), {"query": query})(),
            "query": query,
            "context_need": need,
        },
    )
    final_cov = result.coverage or cov_before
    final_pack = result.prompt_pack or pack_fail

    from adapters.memory import collect_hierarchy_snapshot

    snap = collect_hierarchy_snapshot(
        state=state,
        body=body,
        retrieval_pack=result.retrieval_pack or retrieval,
        coverage=final_cov,
        prompt_pack=final_pack,
    ).to_dict()

    row = HierarchyCase(label="recovery")
    _apply_snap(row, snap, {
        "coverage_complete": bool(getattr(final_cov, "complete", False)),
        "coverage_score": getattr(final_cov, "coverage_score", 0),
        "recovery_rounds": result.rounds,
        "recovery_recovered": result.recovered,
    })
    row.task_success = bool(getattr(final_cov, "complete", False)) and row.coverage_score >= QUALITY_GATES["coverage_score_min"]
    row.recovery_success = bool(result.recovered)
    row.recovery_count = result.rounds
    if not row.task_success:
        row.coverage_fail_reasons = [d.to_dict() for d in analyze_coverage_fail_reasons(need, result.retrieval_pack, final_pack, final_cov)]
    return row


def run_quality_gate_cases() -> list[HierarchyCase]:
    from context_budget import RetrievalStats  # noqa: F401 — used in recovery case

    rows = [
        _run_turn(
            label="bugfix",
            query="fix context_budget.py allocate_dynamic bug",
            intent_name="bugfix",
            setup=_setup_bugfix,
            expected_targets=["context_budget.py", "context_budget.py::allocate_dynamic"],
        ),
        _run_turn(
            label="explore",
            query="explore router memory hierarchy design",
            intent_name="architecture",
            setup=_setup_explore,
            expected_targets=["memory_policy.py", "memory_hierarchy.py"],
        ),
        _run_turn(
            label="recall",
            query="우리가 아까 memory hierarchy 결정 뭐였지?",
            intent_name="recall",
            setup=_setup_recall,
            expected_targets=["previous decision", "session_state", "agent plan"],
        ),
        _run_turn(
            label="doc_analysis",
            query="VISION.md 문서의 memory hierarchy 섹션 분석해줘",
            intent_name="doc_summary",
            setup=_setup_doc_analysis,
            expected_targets=["document", "section", "summary"],
        ),
        _run_recovery_case(),
    ]
    return rows


def run_compression_cases() -> list[HierarchyCase]:
    return [
        _run_turn(label="bugfix", query="fix context_budget.py allocate_dynamic bug", intent_name="bugfix", setup=_setup_bugfix),
        _run_turn(label="explore", query="explore router memory hierarchy design", intent_name="architecture", setup=_setup_explore),
    ]


def _print_table(rows: list[HierarchyCase], *, quality: bool = False) -> None:
    if quality:
        print(
            f"{'label':<14} {'raw':>8} {'stored':>7} {'retr':>7} {'pack':>7} {'gpu':>7} "
            f"{'ratio':>7} {'cov':>6} {'task':>5} {'recv':>5} {'reread':>7}"
        )
        print("-" * 98)
        for r in rows:
            print(
                f"{r.label:<14} {r.raw_tokens:>8} {r.stored_items:>7} {r.retrieved_tokens:>7} "
                f"{r.prompt_pack_tokens:>7} {r.gpu_context_tokens:>7} {r.ratio:>7.3f} "
                f"{r.coverage_score:>6.2f} {'OK' if r.task_success else 'FAIL':>5} "
                f"{r.recovery_count:>5} {r.repeated_read_avoidance:>7.2f}"
            )
    else:
        print(
            f"{'label':<14} {'raw':>8} {'stored':>8} {'retr':>8} {'pack':>8} "
            f"{'gpu':>8} {'ratio':>7} {'hit':>6} {'reread':>7} {'cov':>6}"
        )
        print("-" * 92)
        for r in rows:
            print(
                f"{r.label:<14} {r.raw_tokens:>8} {r.stored_memory_tokens:>8} "
                f"{r.retrieved_tokens:>8} {r.prompt_pack_tokens:>8} {r.gpu_context_tokens:>8} "
                f"{r.ratio:>7.3f} {r.memory_hit_rate:>6.2f} {r.repeated_read_avoidance:>7.2f} "
                f"{r.coverage_score:>6.2f}"
            )


def _print_failures(rows: list[HierarchyCase]) -> None:
    for r in rows:
        if r.task_success and (r.label != "recovery" or r.recovery_success):
            continue
        print(f"\n--- FAIL breakdown: {r.label} ---")
        if r.missing_must_include:
            print(f"  missing_must_include: {r.missing_must_include}")
        for detail in r.coverage_fail_reasons:
            print(
                f"  - {detail['item']}: {detail['reason']} "
                f"(tier={detail.get('tier')}, budget_truncated={detail.get('budget_truncated')})"
            )


def _evaluate_quality_gate(rows: list[HierarchyCase]) -> tuple[bool, dict[str, Any]]:
    task_ok = sum(1 for r in rows if r.task_success) / max(len(rows), 1)
    recovery_rows = [r for r in rows if r.label == "recovery"]
    recovery_ok = (
        sum(1 for r in recovery_rows if r.recovery_success) / len(recovery_rows)
        if recovery_rows
        else 1.0
    )
    ratios = [r.ratio for r in rows if r.raw_tokens > 0]
    coverages = [r.coverage_score for r in rows]
    rereads = [r.repeated_read_avoidance for r in rows]

    summary = {
        "raw_to_gpu_ratio_max": max(ratios) if ratios else 0.0,
        "coverage_score_min": min(coverages) if coverages else 0.0,
        "coverage_score_avg": sum(coverages) / len(coverages) if coverages else 0.0,
        "task_success": task_ok,
        "recovery_success": recovery_ok,
        "repeated_read_avoidance_min": min(rereads) if rereads else 1.0,
        "repeated_read_avoidance_avg": sum(rereads) / len(rereads) if rereads else 1.0,
    }

    passed = (
        summary["raw_to_gpu_ratio_max"] <= QUALITY_GATES["raw_to_gpu_ratio_max"]
        and summary["coverage_score_min"] >= QUALITY_GATES["coverage_score_min"]
        and summary["task_success"] >= QUALITY_GATES["task_success_min"]
        and summary["recovery_success"] >= QUALITY_GATES["recovery_success_min"]
        and summary["repeated_read_avoidance_min"] >= QUALITY_GATES["repeated_read_avoidance_min"]
    )
    return passed, summary


def _run_repeated_read_benchmark() -> dict[str, Any]:
    """Delegate to dedicated repeated-read gate script."""
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "benchmark-repeated-read-avoidance.py"), "--json"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    if proc.returncode != 0 and proc.stdout:
        print(proc.stdout)
    payload = json.loads(proc.stdout or "{}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Memory hierarchy benchmark")
    parser.add_argument("--quality-gate", action="store_true", help="Run 5-case quality gate")
    parser.add_argument("--repeated-read", action="store_true", help="Run repeated-read avoidance bench")
    args = parser.parse_args()

    if args.quality_gate:
        rows = run_quality_gate_cases()
        print("=== memory hierarchy quality gate ===\n")
        _print_table(rows, quality=True)
        passed, summary = _evaluate_quality_gate(rows)
        print("\n--- gate summary ---")
        for k, v in summary.items():
            target = QUALITY_GATES.get(k.replace("_min", "_min").replace("_max", "_max"))
            if k.endswith("_min") or k.endswith("_max"):
                gate_key = k if k in QUALITY_GATES else None
                suffix = f" (gate: {QUALITY_GATES[gate_key]})" if gate_key else ""
            else:
                suffix = ""
            if k == "task_success":
                suffix = f" (gate: >={QUALITY_GATES['task_success_min']:.0%})"
            elif k == "recovery_success":
                suffix = f" (gate: >={QUALITY_GATES['recovery_success_min']:.0%})"
            print(f"  {k}: {v:.4f}{suffix}")
        _print_failures(rows)

        out = ROOT / "tmp" / "benchmark-memory-hierarchy-quality.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cases": [asdict(r) for r in rows], "summary": summary, "passed": passed}
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwritten: {out}")

        if passed and args.repeated_read:
            rr = _run_repeated_read_benchmark()
            rr_out = ROOT / "tmp" / "benchmark-repeated-read-avoidance.json"
            rr_out.write_text(json.dumps(rr, indent=2), encoding="utf-8")
            print("\n=== repeated read avoidance (post quality gate) ===")
            print(f"  live: {rr.get('live_avoidance', 0):.2f}")
            print(f"  stress: {rr.get('stress_avoidance', 0):.2f}")
            print(f"  written: {rr_out}")

        print("\nQUALITY GATE PASS" if passed else "\nQUALITY GATE FAIL")
        return 0 if passed else 1

    rows = run_compression_cases()
    print("=== memory hierarchy benchmark ===\n")
    _print_table(rows)
    out = ROOT / "tmp" / "benchmark-memory-hierarchy.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([asdict(r) for r in rows], indent=2), encoding="utf-8")
    print(f"\nwritten: {out}")
    ok = all(r.prompt_pack_tokens > 0 for r in rows)
    ok = ok and all(r.gpu_context_tokens <= max(r.prompt_pack_tokens * 2, 1) for r in rows)
    ok = ok and all(r.raw_tokens >= r.gpu_context_tokens for r in rows)
    print("ALL OK" if ok else "SOME FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
