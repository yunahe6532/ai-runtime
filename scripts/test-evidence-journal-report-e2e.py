#!/usr/bin/env python3
"""Phase 1.8 E2E — EvidenceAnchor ingest, Task Journal, Final Report."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from legacy.memory_store import Artifact, SessionState  # noqa: E402
from reference.response_guard import build_partial_final_prose  # noqa: E402
from runtime_kernel.evidence_anchor import content_hash, upsert_anchor  # noqa: E402
from runtime_kernel.evidence_ingest import anchor_from_artifact, ingest_artifacts_evidence  # noqa: E402
from runtime_kernel.evidence_anchor import EvidenceAnchor  # noqa: E402
from runtime_kernel.final_report import render_final_report  # noqa: E402
from runtime_kernel.memory_limits import (  # noqa: E402
    MAX_ANCHORS_PER_FILE,
    MAX_JOURNAL_EVENTS,
    prune_anchors,
    prune_journal,
)
from runtime_kernel.task_journal import (  # noqa: E402
    JournalKind,
    build_handoff,
    render_handoff_markdown,
)


def _artifact(
    tmp: Path,
    *,
    art_id: str,
    art_type: str,
    name: str,
    path: str = "",
    text: str = "",
    command: str = "",
    summary: str = "",
    prompt_excerpt: str = "",
) -> Artifact:
    raw = tmp / f"{art_id}.txt"
    raw.write_text(text, encoding="utf-8")
    return Artifact(
        artifact_id=art_id,
        req_id="e2e",
        delta_id="d1",
        type=art_type,
        name=name,
        path=path,
        raw_path=str(raw),
        command=command,
        chars=len(text),
        summary=summary or "summary",
        prompt_excerpt=prompt_excerpt or summary or "excerpt",
    )


def _fresh_state(**kwargs) -> SessionState:
    s = SessionState()
    s.current_query = kwargs.get("query", "e2e test query")
    s.files_read = list(kwargs.get("files_read") or [])
    s.commands_run = list(kwargs.get("commands_run") or [])
    s.agent_plan = dict(kwargs.get("agent_plan") or {"evidence_collected": []})
    return s


def test_read_ingest(tmp: Path) -> None:
    text = "   42|def main():\n   43|    return 0\n"
    art = _artifact(
        tmp,
        art_id="read1",
        art_type="file_read",
        name="Read",
        path="router/main.py",
        text=text,
        summary="read main entry",
    )
    state = _fresh_state(files_read=["router/main.py"])
    n = ingest_artifacts_evidence(state, [art], query="find main")
    assert n == 1
    anchors = state.evidence_anchors
    assert len(anchors) == 1
    a = anchors[0]
    assert a["path"] == "router/main.py"
    assert a["line_start"] == 42
    assert a["line_end"] == 43
    assert a["summary"]
    assert a["content_hash"] == content_hash(text)
    journal = [j for j in state.task_journal if j.get("kind") == JournalKind.READ.value]
    assert len(journal) == 1
    assert journal[0]["target"] == "router/main.py"
    print("PASS test_read_ingest")


def test_grep_ingest(tmp: Path) -> None:
    text = (
        "<workspace_result workspace_path=\"/proj\">\n"
        "router/main.py\n"
        "  10:def bootstrap():\n"
        "router/util.py\n"
        "  3:import os\n"
        "</workspace_result>"
    )
    art = _artifact(
        tmp,
        art_id="grep1",
        art_type="tool_result",
        name="Grep",
        text=text,
        summary="grep bootstrap",
    )
    state = _fresh_state()
    n = ingest_artifacts_evidence(
        state,
        [art],
        query="find bootstrap",
    )
    assert n == 1
    a = state.evidence_anchors[0]
    assert "main.py" in a["path"]
    assert a["why_read"].startswith("grep")
    grep_j = [j for j in state.task_journal if j.get("kind") == JournalKind.GREP.value]
    assert len(grep_j) == 1
    print("PASS test_grep_ingest")


def test_edit_ingest(tmp: Path) -> None:
    art = _artifact(
        tmp,
        art_id="edit1",
        art_type="tool_result",
        name="StrReplace",
        path="router/config.py",
        text="The file has been updated.",
        summary="patched config",
    )
    state = _fresh_state(files_read=["router/config.py"])
    n = ingest_artifacts_evidence(state, [art], query="fix config")
    assert n == 1
    a = state.evidence_anchors[0]
    assert a["path"] == "router/config.py"
    assert a["why_read"] == "code edit"
    edits = [j for j in state.task_journal if j.get("kind") == JournalKind.EDIT.value]
    assert len(edits) == 1
    assert edits[0]["target"] == "router/config.py"
    print("PASS test_edit_ingest")


def test_shell_ingest(tmp: Path) -> None:
    pytest_out = (
        "Command output:\n"
        "============================= test session starts ==============================\n"
        "collected 3 items\n"
        "...\n"
        "============================== 3 passed in 0.12s ===============================\n"
        "Exit code: 0"
    )
    art = _artifact(
        tmp,
        art_id="shell1",
        art_type="shell_result",
        name="Shell",
        text=pytest_out,
        command="python3 -m pytest scripts/test-api.py -q",
        summary="pytest pass",
    )
    state = _fresh_state(commands_run=["python3 -m pytest scripts/test-api.py -q"])
    n = ingest_artifacts_evidence(state, [art], query="run tests")
    assert n == 1
    a = state.evidence_anchors[0]
    assert a["meta"]["exit_ok"] is True
    shells = [j for j in state.task_journal if j.get("kind") == JournalKind.SHELL.value]
    assert len(shells) == 1
    assert shells[0]["meta"]["success"] is True
    successes = [j for j in state.task_journal if j.get("kind") == JournalKind.SUCCESS.value]
    assert len(successes) == 1
    report = render_final_report(state, query="run tests")
    assert "Commands & Test Results" in report or "pytest" in report.lower()
    print("PASS test_shell_ingest")


def test_final_report_render(tmp: Path) -> None:
    state = _fresh_state(
        files_read=["a.py", "b.py"],
        agent_plan={"evidence_collected": ["tree_seen"], "missing_evidence": ["tests_run"]},
    )
    ingest_artifacts_evidence(
        state,
        [
            _artifact(
                tmp,
                art_id="r2",
                art_type="file_read",
                name="Read",
                path="a.py",
                text="   1|# header\n",
            )
        ],
        query="analyze project",
    )
    build_handoff(state, query="analyze project")
    report = render_final_report(state, query="analyze project")
    assert report.startswith("# Task Report")
    assert "Evidence Anchors" in report
    assert "Task Journal" in report
    assert "a.py" in report
    assert "tree_seen" in report or "Evidence Collected" in report
    print("PASS test_final_report_render")


def test_handoff_render(tmp: Path) -> None:
    state = _fresh_state(files_read=["router/x.py"])
    ingest_artifacts_evidence(
        state,
        [
            _artifact(
                tmp,
                art_id="r3",
                art_type="file_read",
                name="Read",
                path="router/x.py",
                text="no line numbers here",
            )
        ],
        query="continue task",
    )
    ho = build_handoff(state, query="continue task")
    assert ho["query"] == "continue task"
    assert ho["journal_count"] >= 1
    assert ho["anchor_count"] >= 1
    assert ho["journal_tail"]
    md = render_handoff_markdown(ho)
    assert "[Task Handoff]" in md
    assert "continue task" in md
    print("PASS test_handoff_render")


def test_duplicate_anchor_upsert(tmp: Path) -> None:
    text = "   5|x = 1\n"
    art = _artifact(
        tmp,
        art_id="dup1",
        art_type="file_read",
        name="Read",
        path="dup.py",
        text=text,
    )
    state = _fresh_state()
    ingest_artifacts_evidence(state, [art], query="dup")
    ingest_artifacts_evidence(state, [art], query="dup")
    keys = [a.get("anchor_key") for a in state.evidence_anchors]
    assert len(keys) == len(set(keys))
    anchor = anchor_from_artifact(art, raw_text=text, query="dup")
    assert anchor is not None
    upsert_anchor(state, anchor)
    upsert_anchor(state, anchor)
    keys2 = [a.get("anchor_key") for a in state.evidence_anchors]
    assert len(keys2) == len(set(keys2))
    print("PASS test_duplicate_anchor_upsert")


def test_lineless_fallback_anchor(tmp: Path) -> None:
    text = "plain file content without line numbers"
    art = _artifact(
        tmp,
        art_id="plain1",
        art_type="file_read",
        name="Read",
        path="plain.txt",
        text=text,
    )
    anchor = anchor_from_artifact(art, raw_text=text, query="read plain")
    assert anchor is not None
    assert anchor.path == "plain.txt"
    assert anchor.line_start == 0
    assert anchor.line_end == 0
    assert anchor.content_hash == content_hash(text)
    state = _fresh_state()
    n = ingest_artifacts_evidence(state, [art], query="read plain")
    assert n == 1
    print("PASS test_lineless_fallback_anchor")


def test_memory_caps() -> None:
    journal = [{"kind": "note", "target": str(i)} for i in range(MAX_JOURNAL_EVENTS + 50)]
    pruned = prune_journal(journal)
    assert len(pruned) == MAX_JOURNAL_EVENTS
    assert pruned[-1]["target"] == str(MAX_JOURNAL_EVENTS + 49)

    anchors = [
        {"path": "same.py", "anchor_key": f"k{i}", "summary": f"s{i}"}
        for i in range(MAX_ANCHORS_PER_FILE + 5)
    ]
    pruned_a = prune_anchors(anchors)
    assert len(pruned_a) == MAX_ANCHORS_PER_FILE
    assert pruned_a[-1]["anchor_key"] == f"k{MAX_ANCHORS_PER_FILE + 4}"
    print("PASS test_memory_caps")


def test_final_report_used_flag(tmp: Path) -> None:
    state = _fresh_state(
        files_read=["router/main.py"],
        agent_plan={"evidence_collected": ["core_files_seen", "project_tree_seen"]},
    )
    for i in range(3):
        ingest_artifacts_evidence(
            state,
            [
                _artifact(
                    tmp,
                    art_id=f"fr{i}",
                    art_type="file_read",
                    name="Read",
                    path=f"router/m{i}.py",
                    text=f"   {i}|def f():\n   {i+1}|    pass\n",
                    summary=f"read m{i}",
                )
            ],
            query="full task report",
        )
    build_handoff(state, query="full task report")
    prose = build_partial_final_prose(
        "full task report",
        session_state=state,
        reason="empty_outgoing",
    )
    assert "# Task Report" in prose
    assert "요청 처리 중" not in prose[:80] or "# Task Report" in prose
    rt = state.last_runtime_turn or {}
    assert rt.get("final_report_used") is True
    assert int(rt.get("final_report_chars") or 0) > 200
    # fallback path when report too short
    empty = _fresh_state()
    prose_fb = build_partial_final_prose("tiny", session_state=empty, reason="loop")
    assert prose_fb
    rt2 = empty.last_runtime_turn or {}
    assert rt2.get("final_report_used") is False
    print("PASS test_final_report_used_flag")


def test_inspector_memory_section() -> None:
    from runtime_inspector import RuntimeInspectorContext, build_runtime_inspector

    state = SessionState()
    state.task_journal = [{"kind": "read", "target": "a.py", "summary": "ok"}]
    state.evidence_anchors = [{"path": "a.py", "line_start": 1, "summary": "x"}]
    state.handoff = {"updated_at": "2026-06-22T00:00:00Z"}
    state.last_runtime_turn = {"final_report_used": True, "final_report_chars": 900}
    ctx = RuntimeInspectorContext(
        run_id="e2e",
        phase="final_answer",
        query="q",
        agent_plan={},
        session_state=state,
    )
    md = build_runtime_inspector(ctx)
    assert "Journal events: 1" in md
    assert "Evidence anchors: 1" in md
    assert "Final report: used=True" in md
    print("PASS test_inspector_memory_section")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_read_ingest(tmp)
        test_grep_ingest(tmp)
        test_edit_ingest(tmp)
        test_shell_ingest(tmp)
        test_final_report_render(tmp)
        test_handoff_render(tmp)
        test_duplicate_anchor_upsert(tmp)
        test_lineless_fallback_anchor(tmp)
        test_memory_caps()
        test_final_report_used_flag(tmp)
        test_inspector_memory_section()
    print("\nAll evidence/journal/report E2E tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
