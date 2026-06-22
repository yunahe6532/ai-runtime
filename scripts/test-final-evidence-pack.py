#!/usr/bin/env python3
"""final_answer must load session artifacts and pool evidence budget."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from adapters.memory import Artifact, SessionState  # noqa: E402
from context_budget import allocate_static  # noqa: E402
import legacy.memory_store as memory_store  # noqa: E402
import legacy.retriever as retriever_mod  # noqa: E402
from prompt_builder import (  # noqa: E402
    _final_answer_evidence_budget,
    _load_session_evidence_artifacts,
    _pack_final_answer_evidence,
    build_with_budget,
    estimate_text_tokens,
)


def _write_artifact(state: SessionState, art: Artifact) -> None:
    adir = memory_store.ARTIFACT_DIR
    adir.mkdir(parents=True, exist_ok=True)
    glob_body = (
        f"Result of search in '{art.path}' (total 3 files):\n"
        "- alpha.py\n- beta.py\n- gamma.py\n"
    )
    raw = adir / f"{art.artifact_id}.txt"
    raw.write_text(glob_body, encoding="utf-8")
    art.raw_path = str(raw)
    art.prompt_excerpt = "- alpha.py\n- beta.py\n- gamma.py"
    meta = adir / f"{art.artifact_id}.json"
    meta.write_text(json.dumps(art.__dict__, ensure_ascii=False), encoding="utf-8")
    if art.artifact_id not in state.artifacts:
        state.artifacts.append(art.artifact_id)


def test_load_session_evidence_not_delta_only() -> None:
    state = SessionState(project_key="test", chat_id="c1")
    a1 = Artifact(
        artifact_id="art1",
        req_id="r1",
        delta_id="d1",
        type="file_read",
        name="Glob",
        path="/proj/runtime_core",
        chat_id="c1",
    )
    a2 = Artifact(
        artifact_id="art2",
        req_id="r2",
        delta_id="d2",
        type="file_read",
        name="Glob",
        path="/proj/adapters",
        chat_id="c1",
    )
    _write_artifact(state, a1)
    _write_artifact(state, a2)

    loaded = _load_session_evidence_artifacts(state, artifacts=[])
    ids = {a.artifact_id for a in loaded}
    assert ids == {"art1", "art2"}, ids
    print("test_load_session_evidence_not_delta_only: OK")


def test_final_answer_pools_evidence_budget() -> None:
    plan = allocate_static("fast", "final_answer", 4096)
    pooled = _final_answer_evidence_budget(plan)
    assert pooled > plan.retrieved, (pooled, plan.retrieved)
    assert pooled >= int(plan.total * 0.75), pooled
    print("test_final_answer_pools_evidence_budget: OK")


def test_pack_final_answer_uses_session_artifacts() -> None:
    state = SessionState(project_key="test", chat_id="c1")
    arts = []
    for i, sub in enumerate(("runtime_core", "adapters", "legacy", "integrations")):
        art = Artifact(
            artifact_id=f"art{i}",
            req_id=f"r{i}",
            delta_id=f"d{i}",
            type="file_read",
            name="Glob",
            path=f"/proj/{sub}",
            chat_id="c1",
        )
        _write_artifact(state, art)
        arts.append(art)

    budget = 12000
    block = _pack_final_answer_evidence(
        [],
        budget,
        phase="final_answer",
        coverage_targets=[f"dir.{s}" for s in ("runtime_core", "adapters", "legacy", "integrations")],
        state=state,
    )
    assert "[collected_evidence]" in block
    assert "runtime_core" in block
    assert "legacy" in block
    assert "integrations" in block
    used = estimate_text_tokens(block)
    assert used >= 150, used
    assert block.count("### ") >= 4, block
    print(f"test_pack_final_answer_uses_session_artifacts: OK used_est={used}")


def test_build_with_budget_final_strips_tool_history() -> None:
    state = SessionState(project_key="test", chat_id="c1")
    art = Artifact(
        artifact_id="artx",
        req_id="rx",
        delta_id="dx",
        type="file_read",
        name="Glob",
        path="/proj/runtime_core",
        chat_id="c1",
    )
    _write_artifact(state, art)

    body = {
        "model": "m",
        "max_tokens": 4096,
        "messages": [
            {"role": "user", "content": "<user_query>구조 요약</user_query>"},
            {"role": "assistant", "content": "tool call noise"},
            {"role": "tool", "name": "Glob", "content": "old tail"},
        ],
    }
    plan = allocate_static("fast", "final_answer", 4096)
    pack = build_with_budget(
        body=body,
        state=state,
        delta=__import__("adapters.memory", fromlist=["RequestDelta"]).RequestDelta(
            delta_id="d",
            req_id="r",
            prev_req_id=None,
            prev_message_count=0,
            curr_message_count=3,
            added_count=0,
        ),
        artifacts=[],
        intent_name="read_only_analysis",
        phase="final_answer",
        backend="fast",
        index=__import__("context_cache", fromlist=["ContextIndex"]).ContextIndex(
            query="구조 요약",
            req_id="r",
            raw_tokens=100,
            message_count=3,
            tool_count=1,
            tool_names=[],
        ),
        query="구조 요약",
        budget_plan=plan,
    )
    msgs = pack.body.get("messages") or []
    roles = [m.get("role") for m in msgs]
    assert roles == ["system", "user"], roles
    sys_text = msgs[0].get("content", "")
    assert "[collected_evidence]" in sys_text or "runtime_core" in sys_text
    assert "tool call noise" not in sys_text
    print("test_build_with_budget_final_strips_tool_history: OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        adir = Path(td) / "artifacts"
        memory_store.ARTIFACT_DIR = adir
        memory_store.CACHE_DIR = Path(td)
        retriever_mod.ARTIFACT_DIR = adir
        test_load_session_evidence_not_delta_only()
        test_final_answer_pools_evidence_budget()
        test_pack_final_answer_uses_session_artifacts()
        test_build_with_budget_final_strips_tool_history()
    print("ALL OK")
