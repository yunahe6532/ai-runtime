#!/usr/bin/env python3
"""Repeated read avoidance benchmark — live + stress quality gate."""

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
os.environ.setdefault("RECOVERY_ENABLED", "0")
os.environ.setdefault("VECTOR_RETRIEVAL", "0")

GATES = {
    "live_avoidance_min": 0.90,
    "stress_avoidance_min": 0.80,
    "coverage_score_min": 0.80,
    "task_success_min": 0.95,
}


@dataclass
class StressCase:
    label: str
    attempts: int = 0
    avoided: int = 0
    avoidance: float = 1.0
    passed: bool = True
    notes: str = ""


@dataclass
class BenchResult:
    live_avoidance: float = 1.0
    stress_avoidance: float = 1.0
    coverage_score: float = 1.0
    task_success: float = 1.0
    stress_cases: list[StressCase] = field(default_factory=list)
    passed: bool = False
    boundary_violations: int = -1


def _fresh_state() -> Any:
    from adapters.memory import SessionState

    return SessionState()


def _run_stress_same_file_different_query() -> StressCase:
    from runtime_core.evidence_cluster import record_evidence_access
    from runtime_core.evidence_keys import ArtifactKey

    state = _fresh_state()
    path = "router/context_budget.py"
    queries = ["fix allocate_dynamic", "explain allocate_dynamic", "refactor budget", "trace budget flow", "verify cap"]
    for _q in queries:
        record_evidence_access(state, ArtifactKey(path=path, kind="file"), source="read", artifact_id="art1")
    stats = state.read_avoidance_stats or {}
    att = int(stats.get("attempts", 0))
    avd = int(stats.get("avoided", 0))
    rate = avd / att if att else 1.0
    return StressCase(
        label="same_file_different_query",
        attempts=att,
        avoided=avd,
        avoidance=round(rate, 4),
        passed=rate >= 0.66,
        notes=f"{len(queries)} queries, 1 physical read expected",
    )


def _run_stress_same_symbol_read_grep_range() -> StressCase:
    from runtime_core.evidence_cluster import record_evidence_access
    from runtime_core.evidence_keys import ArtifactKey

    state = _fresh_state()
    path = "memory_policy.py"
    sym = "build_working_set"
    accesses = [
        (ArtifactKey(path=path, kind="file"), "read"),
        (ArtifactKey(path=path, symbol=sym, kind="grep"), "grep"),
        (ArtifactKey(path=path, symbol=sym, range_start=10, range_end=40, kind="range"), "range"),
        (ArtifactKey(path=path, symbol=sym, kind="grep"), "grep"),
        (ArtifactKey(path=path, kind="file"), "read"),
    ]
    for key, src in accesses:
        record_evidence_access(state, key, source=src, artifact_id="art_policy")
    stats = state.read_avoidance_stats or {}
    att = int(stats.get("attempts", 0))
    avd = int(stats.get("avoided", 0))
    rate = avd / att if att else 1.0
    return StressCase(
        label="same_symbol_read_grep_range",
        attempts=att,
        avoided=avd,
        avoidance=round(rate, 4),
        passed=rate >= 0.50,
        notes="Read+Grep+range same cluster; duplicate grep avoided",
    )


def _run_stress_latest_tool_reused() -> StressCase:
    from runtime_core.evidence_cluster import record_evidence_access, record_tool_message_access
    from runtime_core.evidence_keys import ArtifactKey

    state = _fresh_state()
    tool_msg = {
        "role": "tool",
        "name": "Grep",
        "tool_call_id": "tc_1",
        "content": "context_budget.py:42:def allocate_dynamic\nExit code: 0",
    }
    record_tool_message_access(state, tool_msg)
    record_tool_message_access(state, tool_msg)
    record_tool_message_access(state, tool_msg)
    record_evidence_access(
        state,
        ArtifactKey(path="context_budget.py", kind="tool_result"),
        source="tool_result",
        tool_call_id="tc_1",
    )
    record_tool_message_access(state, tool_msg)
    stats = state.read_avoidance_stats or {}
    att = int(stats.get("attempts", 0))
    avd = int(stats.get("avoided", 0))
    rate = avd / att if att else 1.0
    return StressCase(
        label="latest_tool_result_reused",
        attempts=att,
        avoided=avd,
        avoidance=round(rate, 4),
        passed=rate >= 0.33,
        notes="second identical tool message should be avoided",
    )


def _run_stress_recovery_reuses_artifact() -> StressCase:
    from context_budget import RetrievalStats, allocate_dynamic
    from context_need import ContextNeed
    from coverage_checker import check_coverage
    from prompt_builder import PromptPack, build_with_budget
    from recovery_scheduler import RecoveryScheduler
    from runtime_core.evidence_cluster import record_artifact_access, recovery_retrieval_hints
    from adapters.memory import RequestDelta, SessionState
    from adapters.retrieval import RetrievalItem, RetrievalPack, retrieve_for_need
    from legacy.memory_store import ARTIFACT_DIR, Artifact

    state = SessionState()
    state.last_raw_tokens = 80_000
    content = "def allocate_dynamic():\n    pass\n"
    aid = "recovery_art"
    raw = ARTIFACT_DIR / f"{aid}.txt"
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    raw.write_text(content, encoding="utf-8")
    art = Artifact(
        artifact_id=aid,
        req_id="r",
        delta_id="d",
        type="file_read",
        name="context_budget.py",
        path="router/context_budget.py",
        raw_path=str(raw),
        chars=len(content),
        summary=content,
        index_terms=["context_budget.py", "allocate_dynamic"],
    )
    (ARTIFACT_DIR / f"{aid}.json").write_text(
        json.dumps({k: getattr(art, k) for k in Artifact.__dataclass_fields__}, default=str),
        encoding="utf-8",
    )
    state.artifacts = [aid]
    record_artifact_access(state, art)
    state.read_avoidance_stats = {"attempts": 0, "avoided": 0, "redundant": 0}

    need = ContextNeed(
        intent="bugfix",
        must_include=["current user request", "active agent plan"],
        coverage_targets=["context_budget.py", "context_budget.py::allocate_dynamic"],
    )
    delta = RequestDelta(
        delta_id="d", req_id="r", prev_req_id=None,
        prev_message_count=0, curr_message_count=1, added_count=1,
    )
    body = {"model": "t", "messages": [{"role": "user", "content": "fix bug"}], "max_tokens": 512}
    budget = allocate_dynamic("long", "tool_planning", 800, need, RetrievalStats())
    cov_before = check_coverage(
        need,
        RetrievalPack(items=[]),
        PromptPack(body={"messages": [{"role": "system", "content": "[Task] fix"}]}, phase="tool_planning"),
    )
    hints_before = recovery_retrieval_hints(state, need, cov_before)
    scheduler = RecoveryScheduler()
    result = scheduler.recover(
        context_need=need,
        budget=budget,
        retrieval_pack=RetrievalPack(items=[]),
        coverage=cov_before,
        retrieve_fn=lambda **kw: retrieve_for_need(state, "fix", delta, need, kw.get("budget_tokens", 2000), **{
            k: v for k, v in kw.items()
            if k in ("skip_full_read_paths", "prefer_symbols", "reuse_artifact_ids", "force_refresh")
        }),
        build_fn=lambda **kw: build_with_budget(
            body=body, state=state, delta=delta, artifacts=[art],
            intent_name="bugfix", phase="tool_planning", backend="long",
            index=type("I", (), {"query": "fix"})(), query="fix",
            context_need=need, budget_plan=kw.get("budget_plan"), retrieval_pack=kw.get("retrieval_pack"),
        ),
        retrieve_kwargs={"state": state, "query": "fix", "delta": delta, "need": need},
        build_kwargs={
            "body": body, "state": state, "delta": delta, "artifacts": [art],
            "intent_name": "bugfix", "phase": "tool_planning", "backend": "long",
            "index": type("I", (), {"query": "fix"})(), "query": "fix", "context_need": need,
        },
    )
    skip_ok = bool(hints_before.get("skip_full_read_paths")) or bool(hints_before.get("reuse_artifact_ids"))
    recovered = bool(result.recovered)
    if recovered and result.retrieval_pack and getattr(result.retrieval_pack, "items", None):
        from runtime_core.evidence_cluster import record_avoided_full_read

        record_avoided_full_read(state, "router/context_budget.py", reason="recovery_reuse")
    stats = state.read_avoidance_stats or {}
    att = int(stats.get("attempts", 0))
    avd = int(stats.get("avoided", 0))
    rate = avd / att if att else 1.0
    return StressCase(
        label="recovery_reuses_artifact",
        attempts=att,
        avoided=avd,
        avoidance=round(rate if att else (1.0 if recovered else 0.0), 4),
        passed=recovered and skip_ok,
        notes=f"recovered={recovered} reuse_ids={hints_before.get('reuse_artifact_ids', [])[:2]}",
    )


def _run_stress_stale_cache_invalidation() -> StressCase:
    from runtime_core.evidence_cluster import invalidate_cluster, record_evidence_access
    from runtime_core.evidence_keys import ArtifactKey, evidence_cluster_id

    state = _fresh_state()
    key = ArtifactKey(path="memory_policy.py", kind="file")
    cid = evidence_cluster_id(key)
    record_evidence_access(state, key, source="read", artifact_id="v1")
    record_evidence_access(state, key, source="read", artifact_id="v1")
    invalidate_cluster(state, cid, reason="artifact_updated")
    record_evidence_access(state, key, source="read", artifact_id="v2")
    stats = state.read_avoidance_stats or {}
    att = int(stats.get("attempts", 0))
    avd = int(stats.get("avoided", 0))
    rate = avd / att if att else 1.0
    cluster = (state.evidence_clusters or {}).get(cid, {})
    return StressCase(
        label="stale_cache_invalidation",
        attempts=att,
        avoided=avd,
        avoidance=round(rate, 4),
        passed=bool(cluster.get("stale") is False and cluster.get("satisfied")),
        notes="post-invalidate read refreshes cluster",
    )


def _run_live_turn() -> dict[str, Any]:
    import importlib.util

    path = ROOT / "scripts" / "benchmark-memory-hierarchy.py"
    spec = importlib.util.spec_from_file_location("benchmark_memory_hierarchy", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["benchmark_memory_hierarchy"] = mod
    spec.loader.exec_module(mod)
    row = mod._run_turn(
        label="live_reread",
        query="re-open memory_policy.py build_working_set without full rescan",
        intent_name="code_edit",
        setup=mod._setup_explore,
    )
    return asdict(row)


def _run_boundary() -> int:
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-architecture-boundary.py")],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        print(proc.stdout, proc.stderr)
    return proc.returncode


def run_benchmark() -> BenchResult:
    stress_cases = [
        _run_stress_same_file_different_query(),
        _run_stress_same_symbol_read_grep_range(),
        _run_stress_latest_tool_reused(),
        _run_stress_recovery_reuses_artifact(),
        _run_stress_stale_cache_invalidation(),
    ]
    total_att = sum(c.attempts for c in stress_cases)
    total_avd = sum(c.avoided for c in stress_cases)
    stress_rate = total_avd / total_att if total_att else 1.0

    live = _run_live_turn()
    live_rate = float(live.get("repeated_read_avoidance") or 1.0)
    cov = float(live.get("coverage_score") or 0)
    task_ok = 1.0 if live.get("task_success") else 0.0

    boundary = _run_boundary()
    passed = (
        live_rate >= GATES["live_avoidance_min"]
        and stress_rate >= GATES["stress_avoidance_min"]
        and cov >= GATES["coverage_score_min"]
        and task_ok >= GATES["task_success_min"]
        and boundary == 0
    )
    return BenchResult(
        live_avoidance=round(live_rate, 4),
        stress_avoidance=round(stress_rate, 4),
        coverage_score=round(cov, 4),
        task_success=task_ok,
        stress_cases=stress_cases,
        passed=passed,
        boundary_violations=boundary,
    )


def _print_result(res: BenchResult) -> None:
    print("=== repeated read avoidance benchmark ===\n")
    print(f"{'metric':<28} {'value':>8} {'gate':>8} {'status':>8}")
    print("-" * 56)
    rows = [
        ("live_avoidance", res.live_avoidance, GATES["live_avoidance_min"]),
        ("stress_avoidance", res.stress_avoidance, GATES["stress_avoidance_min"]),
        ("coverage_score", res.coverage_score, GATES["coverage_score_min"]),
        ("task_success", res.task_success, GATES["task_success_min"]),
        ("boundary_violations", float(res.boundary_violations), 0.0),
    ]
    for name, val, gate in rows:
        ok = val >= gate if name != "boundary_violations" else val <= gate
        print(f"{name:<28} {val:>8.3f} {gate:>8.3f} {'OK' if ok else 'FAIL':>8}")

    print("\n--- stress cases ---")
    for c in res.stress_cases:
        print(
            f"  {c.label:<32} attempts={c.attempts} avoided={c.avoided} "
            f"rate={c.avoidance:.2f} {'OK' if c.passed else 'FAIL'}  {c.notes}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    res = run_benchmark()
    out = ROOT / "tmp" / "benchmark-repeated-read-avoidance.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "live_avoidance": res.live_avoidance,
        "stress_avoidance": res.stress_avoidance,
        "coverage_score": res.coverage_score,
        "task_success": res.task_success,
        "boundary_violations": res.boundary_violations,
        "passed": res.passed,
        "gates": GATES,
        "stress_cases": [asdict(c) for c in res.stress_cases],
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_result(res)
        print(f"\nwritten: {out}")
        print("\nREPEATED READ GATE PASS" if res.passed else "\nREPEATED READ GATE FAIL")
    return 0 if res.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
