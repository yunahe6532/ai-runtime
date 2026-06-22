#!/usr/bin/env python3
"""Runtime flow E2E — Question → Planner → Working Set → Evidence → Final via Explorer Trace.

Unlike test-prompt-phase-instructions.py (string asserts only), this test drives
build_context_for_turn / process_two_pass and verifies NDJSON explorer trace events.

Usage (two terminals):
  Terminal A: python3 scripts/tail-explorer-flow.py --follow
  Terminal B: python3 scripts/test-runtime-flow-e2e.py

After a run, inspect the graph:
  python3 scripts/tail-explorer-flow.py <trace-path> --graph --from-start
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

# Isolate runtime data paths BEFORE router imports (memory_store reads env at import).
_e2e_data = os.environ.get("FLOW_E2E_DATA_DIR")
if not _e2e_data:
    _e2e_data = tempfile.mkdtemp(prefix="runtime-flow-e2e-")
    os.environ["FLOW_E2E_DATA_DIR"] = _e2e_data
_e2e_root = Path(_e2e_data)
os.environ.setdefault("AI_RUNTIME_DATA_DIR", str(_e2e_root))
os.environ.setdefault("CONTEXT_CACHE_DIR", str(_e2e_root / "context-cache"))
os.environ.setdefault("CONTEXT_CACHE_HOST_DIR", os.environ["CONTEXT_CACHE_DIR"])
os.environ.setdefault("CAPTURE_DIR", str(_e2e_root / "captures"))
os.environ.setdefault("CAPTURE_HOST_DIR", os.environ["CAPTURE_DIR"])

sys.path.insert(0, str(ROOT / "router"))

from explorer_trace import (  # noqa: E402
    CANONICAL_RUNTIME_FLOW_REQUIRED,
    build_flow_graph,
    load_trace_rows,
    trace_event_names,
    verify_flow_subsequence,
    write_explorer_trace,
)
from intent_router import process_two_pass  # noqa: E402
from legacy.memory_store import Artifact, SessionState  # noqa: E402
from reference.response_guard import build_partial_final_prose  # noqa: E402
from runtime_kernel.evidence_ingest import ingest_artifacts_evidence  # noqa: E402


def _body(query: str) -> dict:
    ws = str(ROOT)
    return {
        "model": "model.gguf",
        "stream": True,
        "messages": [
            {
                "role": "system",
                "content": "You are a coding assistant. Answer normally in prose.",
            },
            {
                "role": "user",
                "content": (
                    f"<open_and_recently_viewed_files>\n"
                    f"Workspace Path: {ws}\n"
                    f"</open_and_recently_viewed_files>\n"
                    f"<user_query>\n{query}\n</user_query>"
                ),
            },
        ],
        "tools": [
            {"type": "function", "function": {"name": "Read"}},
            {"type": "function", "function": {"name": "Grep"}},
            {"type": "function", "function": {"name": "Glob"}},
        ],
    }


def _artifact(tmp: Path, text: str = "def main(): pass\n") -> Artifact:
    raw = tmp / "read_main.txt"
    raw.write_text(text, encoding="utf-8")
    return Artifact(
        artifact_id="art_flow_read",
        req_id="flow_e2e",
        delta_id="d1",
        type="file_read",
        name="Read",
        path="router/main.py",
        raw_path=str(raw),
        chars=len(text),
        summary="read router/main.py",
        prompt_excerpt=text[:200],
    )


def _configure_env(tmp: Path, trace: Path) -> None:
    os.environ["EXPLORER_TRACE_ENABLED"] = "1"
    os.environ["EXPLORER_TRACE_PATH"] = str(trace)
    os.environ["EXPLORER_TRACE_STDOUT"] = "0"
    os.environ["EXPLORER_TRACE_FORMAT"] = "ndjson"
    cache = tmp / "context-cache"
    captures = tmp / "captures"
    os.environ["CONTEXT_CACHE_DIR"] = str(cache)
    os.environ["CONTEXT_CACHE_HOST_DIR"] = str(cache)
    os.environ["CAPTURE_DIR"] = str(captures)
    os.environ["CAPTURE_HOST_DIR"] = str(captures)
    for sub in ("deltas", "artifacts", "meta", "projects", "raw", "index"):
        (cache / sub).mkdir(parents=True, exist_ok=True)
    captures.mkdir(parents=True, exist_ok=True)

    import context_cache as cc  # noqa: WPS433
    import legacy.memory_store as ms  # noqa: WPS433

    cc.CACHE_DIR = cache
    cc.RAW_DIR = cache / "raw"
    cc.INDEX_DIR = cache / "index"
    ms.CACHE_DIR = cache
    ms.DELTA_DIR = cache / "deltas"
    ms.ARTIFACT_DIR = cache / "artifacts"
    ms.META_DIR = cache / "meta"
    ms.STATE_FILE = cache / "current_state.json"
    ms.PROJECTS_DIR = cache / "projects"
    ms.REGISTRY_FILE = ms.PROJECTS_DIR / "_registry.json"
    os.environ["PLANNER_SHADOW_MODE"] = "1"
    os.environ["LLM_PLANNER_SHADOW_ENABLED"] = "1"
    os.environ["PLANNER_PROMOTION_GATE_ENABLED"] = "1"
    os.environ["PLANNER_PROMOTION_SHADOW_ONLY"] = "1"
    os.environ["DYNAMIC_BUDGET"] = "1"
    os.environ["MEMORY_STATE_BODY"] = "1"
    # Reset cached trace path in explorer_trace module
    import explorer_trace as et  # noqa: WPS433

    et._active_trace_path = trace  # type: ignore[attr-defined]
    et._boot_logged = False  # type: ignore[attr-defined]


def test_runtime_flow_trace(tmp: Path, trace: Path) -> None:
    _configure_env(tmp, trace)
    query = "코드 수정 말고 router 구조만 읽어서 역할 요약해줘"
    body = _body(query)

    llm_response = json.dumps({
        "action": "read",
        "target_files": ["router/main.py"],
        "reason": "entrypoint for structure summary",
        "confidence": 0.91,
        "risk_flags": [],
        "evidence_needed": ["core_files_seen"],
    })

    with mock.patch(
        "agent_brain.llm_planner._invoke_llm",
        return_value=(llm_response, {"status": "ok"}),
    ):
        proxy, backend, stats, intent, phase = process_two_pass(body, active_backend="long")

    assert proxy.get("messages"), "proxy body must have messages"
    assert intent.intent in ("read_only_analysis", "explain", "project_inspection"), intent.intent
    assert phase in ("tool_planning", "final_answer", ""), phase
    assert stats.req_id, "req_id required for trace correlation"

    # Simulate Read tool completion → evidence + journal trace events
    state = stats.mem_state or SessionState()
    state.current_query = query
    state.phase_hint = phase or "tool_planning"
    state.turn_index = int(getattr(state, "turn_index", 0) or 0) + 1
    art = _artifact(tmp)

    write_explorer_trace(
        "tool.requested",
        phase=state.phase_hint,
        query=query,
        turn_index=state.turn_index,
        tool_name="Read",
        tool_args={"path": "router/main.py"},
        path="router/main.py",
        result_summary="read requested",
        flow_id=stats.req_id,
        req_id=stats.req_id,
    )
    ingest_artifacts_evidence(state, [art], query=query)
    write_explorer_trace(
        "tool.completed",
        phase=state.phase_hint,
        query=query,
        turn_index=state.turn_index,
        tool_name="Read",
        path="router/main.py",
        result_summary=art.summary,
        flow_id=stats.req_id,
        req_id=stats.req_id,
    )

    # Final report renderer path (deterministic prose)
    state.phase_hint = "final_answer"
    prose = build_partial_final_prose(query, session_state=state, reason="flow_e2e")
    assert prose.strip(), "final report or fallback prose required"

    rows = load_trace_rows(trace)
    events = trace_event_names(rows)
    assert events, "trace must contain events after process_two_pass"

    required_core = CANONICAL_RUNTIME_FLOW_REQUIRED
    ok, msg = verify_flow_subsequence(events, required_core)
    assert ok, f"core flow order failed: {msg}\n events={events}"

    for name in (
        "planner.runtime_state.created",
        "planner.shadow.proposed",
        "working_set.created",
        "coverage.checked",
        "memory.journal.appended",
        "memory.evidence.upserted",
        "final_report.rendered",
    ):
        assert name in events, f"missing trace event: {name} in {events}"

    graph = build_flow_graph(rows=rows)
    assert "Sequence check: PASS" in graph, graph
    assert "[✓] Planner" in graph, graph
    assert "[✓] Working Set" in graph, graph

    print("PASS test_runtime_flow_trace")
    print(f"  intent={intent.intent} phase={phase} backend={backend} events={len(events)}")
    print(f"  req_id={stats.req_id}")
    if os.environ.get("FLOW_E2E_PRINT_GRAPH") == "1":
        print("\n" + graph)


def test_tail_graph_cli(tmp: Path, trace: Path) -> None:
    """Verify tail-explorer-flow.py --graph exits 0 on a valid trace."""
    import subprocess

    write_explorer_trace("planner.runtime_state.created", query="q", result_summary="boot")
    write_explorer_trace("planner.shadow.proposed", decision="read", query="q")
    write_explorer_trace("planner.shadow.compared", decision="read", match=True, query="q")
    write_explorer_trace("working_set.created", query="q", result_summary="targets=1")
    write_explorer_trace("coverage.checked", query="q", result_summary="score=0.5")
    write_explorer_trace("memory.journal.appended", target="router/x.py", query="q")

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/tail-explorer-flow.py"),
            str(trace),
            "--graph",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "Explorer Runtime Flow" in proc.stdout
    assert "Sequence check: PASS" in proc.stdout
    print("PASS test_tail_graph_cli")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        trace = tmp / "explorer-trace.ndjson"
        test_runtime_flow_trace(tmp, trace)
        # Fresh trace for CLI smoke
        trace2 = tmp / "graph-cli.ndjson"
        os.environ["EXPLORER_TRACE_PATH"] = str(trace2)
        import explorer_trace as et  # noqa: WPS433

        et._active_trace_path = trace2  # type: ignore[attr-defined]
        test_tail_graph_cli(tmp, trace2)
    print("\nAll runtime flow E2E tests passed.")
    print("Tip: python3 scripts/tail-explorer-flow.py --graph --from-start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
