#!/usr/bin/env python3
"""Retriever backend A/B — same adapters.retrieval API, legacy BM25 vs LlamaIndex."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "router"
sys.path.insert(0, str(ROUTER))

DEFAULT_CACHE = ROOT / "tmp" / "context-cache"
DEFAULT_PROJECT = "e5903e2a81c2"

os.environ.setdefault("CONTEXT_CACHE_DIR", str(DEFAULT_CACHE))
os.environ.setdefault("VECTOR_RETRIEVAL", "1")
os.environ.setdefault("DYNAMIC_BUDGET", "1")
os.environ.setdefault("COVERAGE_CHECK", "1")
os.environ.setdefault("RECOVERY_ENABLED", "0")

from context_need import build_context_need  # noqa: E402
from coverage_checker import check_coverage  # noqa: E402
from prompt_builder import PromptPack  # noqa: E402
from reference.planner import AgentPlan  # noqa: E402
from adapters.memory import RequestDelta, load_state  # noqa: E402
from adapters.retrieval import retrieve_for_need  # noqa: E402

DELTA = RequestDelta(
    delta_id="swap_delta",
    req_id="swap_req",
    prev_req_id=None,
    prev_message_count=0,
    curr_message_count=1,
    added_count=0,
)

QUERY_CASES = [
    ("benchmark runtime score token_threshold", "benchmark"),
    ("agent_exec tool validation guard", "agent_exec"),
    ("context budget allocate dynamic", "context"),
    ("docker shell compose status", "docker"),
    ("flow trace proxy saved percent", "flow"),
    ("memory store delta artifact session", "memory"),
    ("coverage checker recovery scheduler", "coverage"),
    ("planner agent plan evidence", "planner"),
]


@dataclass
class BackendMetrics:
    backend: str
    queries: int
    hits: int
    hit_rate: float
    avg_retrieved_tokens: float
    avg_latency_ms: float
    avg_coverage_score: float
    matrix_pass: str
    errors: list[str]


def _llamaindex_available() -> bool:
    try:
        import llama_index.core  # noqa: F401

        return True
    except ImportError:
        return False


def _run_matrix(env: dict[str, str]) -> str:
    merged = os.environ.copy()
    merged.update(env)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "benchmark-dynamic-budget-matrix.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=merged,
    )
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()
    summary = tail[-1] if tail else f"exit={proc.returncode}"
    return f"{'OK' if proc.returncode == 0 else 'FAIL'} ({summary})"


def _benchmark_backend(
    *,
    backend_name: str,
    llamaindex_enabled: str,
    project_key: str,
) -> BackendMetrics:
    os.environ["LLAMAINDEX_ENABLED"] = llamaindex_enabled
    errors: list[str] = []
    hits = 0
    total_tokens = 0
    total_latency = 0.0
    total_coverage = 0.0

    state = load_state(project_key)
    if not state.project_key:
        state.project_key = project_key

    artifact_count = len(state.artifacts or [])
    if artifact_count < 10:
        errors.append(f"corpus too small: artifacts={artifact_count} (set CONTEXT_CACHE_DIR)")

    for query, expect in QUERY_CASES:
        need = build_context_need(AgentPlan(task_intent="bugfix"), query, "agent")
        t0 = time.perf_counter()
        try:
            pack = retrieve_for_need(state, query, DELTA, need, budget_tokens=4000)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{query[:40]}: {exc}")
            continue
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        total_latency += elapsed_ms
        total_tokens += int(pack.total_tokens or 0)

        if pack.items and (
            expect.lower() in str(getattr(pack.items[0], "path", "") or getattr(pack.items[0], "name", "")).lower()
            or any(
                expect.lower() in str(getattr(i, "path", "") or getattr(i, "name", "")).lower()
                for i in pack.items
            )
        ):
            hits += 1

        cov = check_coverage(
            need,
            pack,
            PromptPack(body={"messages": [{"role": "system", "content": "[Task] x"}]}, phase="tool_planning"),
        )
        total_coverage += float(cov.coverage_score or 0.0)

    n = len(QUERY_CASES)
    matrix_env = {
        "VECTOR_RETRIEVAL": "1",
        "LLAMAINDEX_ENABLED": llamaindex_enabled,
        "RECOVERY_ENABLED": "0",
    }
    matrix_pass = _run_matrix(matrix_env)

    return BackendMetrics(
        backend=backend_name,
        queries=n,
        hits=hits,
        hit_rate=round(hits / n, 3) if n else 0.0,
        avg_retrieved_tokens=round(total_tokens / n, 1) if n else 0.0,
        avg_latency_ms=round(total_latency / n, 2) if n else 0.0,
        avg_coverage_score=round(total_coverage / n, 3) if n else 0.0,
        matrix_pass=matrix_pass,
        errors=errors,
    )


def _print_table(a: BackendMetrics, b: BackendMetrics | None) -> None:
    rows = [
        ("hit_rate", a.hit_rate, b.hit_rate if b else None),
        ("avg_retrieved_tokens", a.avg_retrieved_tokens, b.avg_retrieved_tokens if b else None),
        ("avg_latency_ms", a.avg_latency_ms, b.avg_latency_ms if b else None),
        ("avg_coverage_score", a.avg_coverage_score, b.avg_coverage_score if b else None),
        ("matrix_pass", a.matrix_pass, b.matrix_pass if b else None),
    ]
    if b:
        print(f"{'metric':<22} {'legacy_bm25':>14} {'llamaindex':>14}")
        print("-" * 52)
        for name, va, vb in rows:
            print(f"{name:<22} {str(va):>14} {str(vb):>14}")
    else:
        print(f"backend={a.backend}")
        for name, va, _ in rows:
            print(f"  {name}: {va}")
    if a.errors or (b and b.errors):
        print("\nerrors:")
        for e in a.errors:
            print(f"  [{a.backend}] {e}")
        if b:
            for e in b.errors:
                print(f"  [{b.backend}] {e}")


def main() -> int:
    project = os.getenv("BENCHMARK_PROJECT", DEFAULT_PROJECT)
    mode = os.getenv("RETRIEVER_SWAP_MODE", "ab").lower()

    legacy = _benchmark_backend(
        backend_name="legacy_bm25",
        llamaindex_enabled="0",
        project_key=project,
    )

    if mode == "legacy":
        _print_table(legacy, None)
        out = ROOT / "tmp" / "benchmark-retriever-swap.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"legacy_bm25": asdict(legacy)}, indent=2), encoding="utf-8")
        print(f"\nwritten: {out}")
        return 0 if legacy.matrix_pass.startswith("OK") else 1

    if not _llamaindex_available():
        print("LlamaIndex not installed — legacy_bm25 only")
        _print_table(legacy, None)
        print("\nInstall: .venv-llamaindex or pip install llama-index")
        return 0 if legacy.matrix_pass.startswith("OK") else 1

    llama = _benchmark_backend(
        backend_name="llamaindex",
        llamaindex_enabled="1",
        project_key=project,
    )

    print("=== retriever swap A/B (adapters.retrieval.retrieve_for_need) ===\n")
    _print_table(legacy, llama)

    out = ROOT / "tmp" / "benchmark-retriever-swap.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"legacy_bm25": asdict(legacy), "llamaindex": asdict(llama)}, indent=2),
        encoding="utf-8",
    )
    print(f"\nwritten: {out}")

    ok = legacy.matrix_pass.startswith("OK") and llama.matrix_pass.startswith("OK")
    print("ALL OK" if ok else "SOME FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
