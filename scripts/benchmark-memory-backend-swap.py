#!/usr/bin/env python3
"""Memory backend swap benchmark — legacy vs LangGraph checkpointer/store."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

BACKENDS = ("legacy", "langgraph")

GATES = {
    "quality_gate_exit": 0,
    "repeated_read_exit": 0,
    "boundary_exit": 0,
}


def _run_cmd(
    cmd: list[str],
    *,
    env: dict[str, str],
    label: str,
) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"\n--- FAIL {label} ---")
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def _parse_quality_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_repeated_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _bench_backend(backend: str, *, work_dir: Path) -> dict[str, Any]:
    cache = work_dir / "context-cache"
    langgraph_dir = work_dir / "langgraph"
    cache.mkdir(parents=True, exist_ok=True)
    langgraph_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["MEMORY_BACKEND"] = backend
    env["CONTEXT_CACHE_DIR"] = str(cache)
    env["LANGGRAPH_MEMORY_DIR"] = str(langgraph_dir)
    env["PYTHONPATH"] = str(ROOT / "router") + os.pathsep + env.get("PYTHONPATH", "")

    q_out = work_dir / "quality.json"
    r_out = work_dir / "repeated.json"

    q_code, q_stdout, _ = _run_cmd(
        [sys.executable, str(ROOT / "scripts" / "benchmark-memory-hierarchy.py"), "--quality-gate"],
        env=env,
        label=f"{backend}/quality-gate",
    )
    if (ROOT / "tmp" / "benchmark-memory-hierarchy-quality.json").exists():
        (ROOT / "tmp" / "benchmark-memory-hierarchy-quality.json").replace(q_out)

    rr_code, _, _ = _run_cmd(
        [sys.executable, str(ROOT / "scripts" / "benchmark-repeated-read-avoidance.py"), "--json"],
        env=env,
        label=f"{backend}/repeated-read",
    )
    if (ROOT / "tmp" / "benchmark-repeated-read-avoidance.json").exists():
        (ROOT / "tmp" / "benchmark-repeated-read-avoidance.json").replace(r_out)

    b_code, _, _ = _run_cmd(
        [sys.executable, str(ROOT / "scripts" / "check-architecture-boundary.py")],
        env=env,
        label=f"{backend}/boundary",
    )

    q_payload = _parse_quality_json(q_out)
    r_payload = _parse_repeated_json(r_out)
    summary = q_payload.get("summary") or {}

    latency_ms = 0.0
    if backend == "langgraph":
        m_code, m_out, _ = _run_cmd(
            [
                sys.executable,
                "-c",
                (
                    "import os,sys; sys.path.insert(0,'router'); "
                    f"os.environ['MEMORY_BACKEND']='langgraph'; "
                    f"os.environ['CONTEXT_CACHE_DIR']={str(cache)!r}; "
                    f"os.environ['LANGGRAPH_MEMORY_DIR']={str(langgraph_dir)!r}; "
                    "from adapters.memory import load_state,save_state,get_memory_backend_metrics; "
                    "s=load_state(); s.turn_index=1; save_state(s); "
                    "print(get_memory_backend_metrics()['load_latency_ms']+get_memory_backend_metrics()['save_latency_ms'])"
                ),
            ],
            env=env,
            label=f"{backend}/latency",
        )
        if m_code == 0 and m_out.strip():
            try:
                latency_ms = float(m_out.strip().splitlines()[-1])
            except ValueError:
                latency_ms = 0.0

    return {
        "backend": backend,
        "quality_gate_pass": q_code == 0 and bool(q_payload.get("passed")),
        "repeated_read_pass": rr_code == 0 and bool(r_payload.get("passed")),
        "boundary_violations": b_code,
        "raw_to_gpu_ratio_max": summary.get("raw_to_gpu_ratio_max"),
        "coverage_score_min": summary.get("coverage_score_min"),
        "task_success": summary.get("task_success"),
        "recovery_success": summary.get("recovery_success"),
        "repeated_read_avoidance_min": summary.get("repeated_read_avoidance_min"),
        "live_avoidance": r_payload.get("live_avoidance"),
        "stress_avoidance": r_payload.get("stress_avoidance"),
        "stored_items_avg": _avg_stored_items(q_payload),
        "memory_hit_rate_avg": _avg_hit_rate(q_payload),
        "latency_ms": round(latency_ms, 3),
        "quality_exit": q_code,
        "repeated_exit": rr_code,
    }


def _avg_stored_items(payload: dict[str, Any]) -> float | None:
    cases = payload.get("cases") or []
    vals = [c.get("stored_items") for c in cases if c.get("stored_items") is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


def _avg_hit_rate(payload: dict[str, Any]) -> float | None:
    cases = payload.get("cases") or []
    vals = [c.get("memory_hit_rate") for c in cases if c.get("memory_hit_rate") is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _print_table(rows: list[dict[str, Any]]) -> None:
    cols = [
        "backend",
        "ratio_max",
        "coverage",
        "task",
        "recovery",
        "reread_live",
        "reread_stress",
        "latency_ms",
        "stored",
        "hit_rate",
        "status",
    ]
    print(f"{'backend':<12} {'ratio':>7} {'cov':>6} {'task':>6} {'recv':>6} "
          f"{'live':>6} {'stress':>7} {'lat_ms':>8} {'stored':>7} {'hit':>6} {'status':>8}")
    print("-" * 96)
    for r in rows:
        ok = r.get("quality_gate_pass") and r.get("repeated_read_pass") and r.get("boundary_violations") == 0
        print(
            f"{r['backend']:<12} "
            f"{float(r.get('raw_to_gpu_ratio_max') or 0):>7.3f} "
            f"{float(r.get('coverage_score_min') or 0):>6.2f} "
            f"{float(r.get('task_success') or 0):>6.2f} "
            f"{float(r.get('recovery_success') or 0):>6.2f} "
            f"{float(r.get('live_avoidance') or 0):>6.2f} "
            f"{float(r.get('stress_avoidance') or 0):>7.2f} "
            f"{float(r.get('latency_ms') or 0):>8.1f} "
            f"{str(r.get('stored_items_avg') or '-'):>7} "
            f"{str(r.get('memory_hit_rate_avg') or '-'):>6} "
            f"{'OK' if ok else 'FAIL':>8}"
        )


def main() -> int:
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="mem-backend-swap-") as tmp:
        work = Path(tmp)
        for backend in BACKENDS:
            print(f"\n=== MEMORY_BACKEND={backend} ===")
            row = _bench_backend(backend, work_dir=work / backend)
            results.append(row)

    print("\n=== memory backend swap ===")
    _print_table(results)

    out = ROOT / "tmp" / "benchmark-memory-backend-swap.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"backends": results, "passed": all(
        r.get("quality_gate_pass") and r.get("repeated_read_pass") and r.get("boundary_violations") == 0
        for r in results
    )}
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwritten: {out}")
    print("\nBACKEND SWAP PASS" if payload["passed"] else "\nBACKEND SWAP FAIL")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
