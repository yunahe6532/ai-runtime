#!/usr/bin/env python3
"""Test memory_store delta extraction against saved flow/capture files."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running from repo root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from legacy.memory_store import (  # noqa: E402
    CACHE_DIR,
    PROJECTS_DIR,
    SessionState,
    detect_cursor_summary,
    extract_delta,
    extract_workspace_path,
    ingest_request,
    is_fresh_chat,
    load_state,
    project_key_from_workspace,
    project_paths,
    resolve_session,
    save_state,
    _query_fingerprint,
)

CAPTURE_DIR = ROOT / "tmp" / "cursor-captures"
RAW_DIR = ROOT / "tmp" / "context-cache" / "raw"


def load_body(req_stem: str) -> dict | None:
    req_path = CAPTURE_DIR / f"{req_stem}.request.json"
    if req_path.exists():
        data = json.loads(req_path.read_text(encoding="utf-8"))
        return data.get("body", data)
    raw_path = RAW_DIR / f"{req_stem}.json"
    if raw_path.exists():
        data = json.loads(raw_path.read_text(encoding="utf-8"))
        return data.get("body", data)
    return None


def load_body_from_flow(flow_path: Path) -> dict | None:
    """Reconstruct minimal body from flow stage 1 metadata if request.json missing."""
    req_path = flow_path.with_suffix("").with_suffix(".request.json")
    # flow.json name: 1781741948_0001.flow.json -> request: 1781741948_0001.request.json
    req_path = flow_path.parent / flow_path.name.replace(".flow.json", ".request.json")
    if req_path.exists():
        data = json.loads(req_path.read_text(encoding="utf-8"))
        return data.get("body", data)

    # fallback: use summary if available
    summary_path = flow_path.parent / flow_path.name.replace(".flow.json", ".summary.json")
    if summary_path.exists():
        return None  # can't reconstruct full body
    return None


def _reset_cache() -> None:
    state_file = CACHE_DIR / "current_state.json"
    if state_file.exists():
        state_file.unlink()
    if PROJECTS_DIR.exists():
        import shutil
        shutil.rmtree(PROJECTS_DIR)


def test_project_scenarios() -> bool:
    """Project switch, new chat, cursor summary, and retry delta=0."""
    body_new = load_body("1781744625_0001") or load_body("1781744642_0002")
    body_old = load_body("1781741948_0001")
    body_sum = load_body("1781721070_0006")
    if not body_new:
        print("SKIP: new chat capture missing")
        return True

    _reset_cache()
    ws = extract_workspace_path(body_new)
    pk = project_key_from_workspace(ws)
    print(f"\n=== project scenarios (workspace={ws} key={pk}) ===")

    assert is_fresh_chat(body_new["messages"]), "new chat should have no assistant/tool"
    d1, s1, _ = ingest_request("test_new_1", body_new)
    print(f"  new_chat: reason={s1.session_reason} chat={s1.chat_id} delta=+{d1.added_count}")
    assert s1.session_reason in ("new_chat", "continue")
    assert d1.added_count == 3

    d2, s2, _ = ingest_request("test_new_2", body_new)
    print(f"  retry same 3 msgs: delta=+{d2.added_count} chat={s2.chat_id}")
    assert d2.added_count == 0
    assert s2.chat_id == s1.chat_id

    if body_old:
        ingest_request("test_old_long", body_old)
        state_long = load_state(pk)
        d3, s3, _ = ingest_request("test_new_after_long", body_new)
        print(
            f"  new chat after long session: reason={s3.session_reason} "
            f"prev_artifacts={len(state_long.artifacts)} now={len(s3.artifacts)}"
        )
        assert s3.session_reason == "new_chat"
        assert s3.chat_id != state_long.chat_id

    if body_sum:
        assert detect_cursor_summary(body_sum)
        _reset_cache()
        ingest_request("test_sum_1", body_sum)
        s_sum = load_state(project_key_from_workspace(extract_workspace_path(body_sum)))
        print(f"  cursor summary: reason={s_sum.session_reason} msgs={s_sum.last_message_count}")
        assert s_sum.session_reason == "cursor_summary"

    # Different workspace → new project (same ingest session, no reset)
    body_other = json.loads(json.dumps(body_new))
    ingest_request("test_same_proj_anchor", body_new)
    s_anchor = load_state(pk)
    msgs = body_other["messages"]
    for m in msgs:
        if m.get("role") == "user" and "Workspace Path:" in str(m.get("content", "")):
            m["content"] = str(m["content"]).replace(ws, "/tmp/other-project")
    other_pk = project_key_from_workspace("/tmp/other-project")
    ingest_request("test_other_proj", body_other)
    s_other = load_state(other_pk)
    print(f"  project switch: key={other_pk} reason={s_other.session_reason} from={s_anchor.project_key}")
    assert s_other.session_reason == "new_project"
    assert s_other.project_key == other_pk
    assert s_other.project_key != s_anchor.project_key

    pdir = project_paths(pk)
    print(f"  project dir exists: {pdir.state_file.exists()}")
    return True


def test_chat_reset_clears_poison() -> bool:
    """Dramatic shrink, loop escape, and cursor summary must wipe chat-scoped state."""
    import shutil

    body_long = load_body("1782050444_0028")
    body_new = load_body("1782049778_0001") or load_body("1782045446_0002")
    body_sum = load_body("1781721070_0006")
    if not body_long or not body_new:
        print("SKIP: chat reset captures missing")
        return True

    _reset_cache()
    ws = extract_workspace_path(body_long)
    pk = project_key_from_workspace(ws)

    _, s_long, _ = ingest_request("reset_long", body_long)
    assert len(s_long.artifacts) > 10, "long session should accumulate artifacts"
    assert s_long.chat_id, "chat_id must be assigned during session"

    _, s_new, _ = ingest_request("reset_new", body_new)
    print(
        f"  dramatic shrink: reason={s_new.session_reason} artifacts={len(s_new.artifacts)} "
        f"chat={s_new.chat_id}"
    )
    assert s_new.session_reason == "new_chat"
    assert len(s_new.artifacts) == 0
    assert s_new.chat_id != s_long.chat_id
    assert not (s_new.agent_plan or {}).get("source_hits")

    # Simulate stuck loop then same-query retry
    for i in range(10):
        ingest_request(f"reset_grow_{i}", body_long)
    stuck = load_state(pk)
    stuck.turns_since_progress = 8
    stuck.active_query_hash = _query_fingerprint(stuck.current_query)
    save_state(stuck, pk)

    body_retry = json.loads(json.dumps(body_long))
    msgs = body_retry["messages"]
    uq = stuck.current_query or "retry same task"
    msgs.append({"role": "user", "content": f"<user_query>{uq}</user_query>"})
    _, s_escape, _ = ingest_request("reset_escape", body_retry)
    print(
        f"  loop escape: reason={s_escape.session_reason} artifacts={len(s_escape.artifacts)}"
    )
    assert s_escape.session_reason == "new_chat"
    assert len(s_escape.artifacts) == 0

    if body_sum:
        _reset_cache()
        ingest_request("reset_sum_anchor", body_long)
        _, s_sum, _ = ingest_request("reset_sum", body_sum)
        print(f"  cursor summary reset: reason={s_sum.session_reason} artifacts={len(s_sum.artifacts)}")
        assert s_sum.session_reason == "cursor_summary"
        assert len(s_sum.artifacts) == 0

    return True


def test_delta_sequence():
    """Simulate sequential requests using request.json files if available."""
    req_files = sorted(CAPTURE_DIR.glob("178174*.request.json"))
    if not req_files:
        req_files = sorted(CAPTURE_DIR.glob("*.request.json"))
    if not req_files:
        print("SKIP: no request.json captures found")
        return False

    # Reset state for clean test
    _reset_cache()

    print(f"Testing {len(req_files)} sequential requests\n")
    prev_count = 0
    ok = True

    for req_path in req_files[:6]:
        data = json.loads(req_path.read_text(encoding="utf-8"))
        body = data.get("body", data)
        req_id = data.get("id", req_path.stem.replace(".request", ""))
        messages = body.get("messages", [])

        state = load_state()
        delta = extract_delta(req_id, body, state)

        expected_added = len(messages) - prev_count if prev_count > 0 else len(messages)
        status = "OK" if delta.added_count == expected_added or prev_count == 0 else "WARN"

        print(f"{req_id}: {prev_count} → {len(messages)} (+{delta.added_count}) [{status}]")
        for dm in delta.added:
            extra = f" {dm.tool_calls}" if dm.tool_calls else ""
            print(f"  [{dm.index}] {dm.role} {dm.chars}chars{extra}")

        ingest_request(req_id, body)
        prev_count = len(messages)

    return ok


def test_flow_metadata():
    """Verify flow files show expected +2 pattern."""
    flows = sorted(CAPTURE_DIR.glob("178174*.flow.json"))
    if len(flows) < 2:
        print("SKIP: need at least 2 flow files")
        return True

    print("\n=== Flow message growth ===")
    prev = 0
    for f in flows:
        d = json.loads(f.read_text(encoding="utf-8"))
        s1 = d["stages"][0]
        count = s1["message_count"]
        diff = count - prev if prev else count
        print(f"  {d['id']}: {count} msgs (+{diff}) last={s1['last_role']}")
        prev = count
    return True


def main() -> int:
    print("=== memory_store delta test ===\n")
    ok0 = test_project_scenarios()
    ok1 = test_delta_sequence()
    ok2 = test_flow_metadata()
    ok3 = test_chat_reset_clears_poison()

    state = load_state()
    print(f"\n=== current_state.json ===")
    print(f"  project: {state.project_key} ({state.workspace_path})")
    print(f"  chat: {state.chat_id} reason={state.session_reason}")
    print(f"  last_req_id: {state.last_req_id}")
    print(f"  message_count: {state.last_message_count}")
    print(f"  files_read: {state.files_read[:5]}")
    print(f"  commands_run: {state.commands_run[:5]}")
    print(f"  artifacts: {len(state.artifacts)}")

    if PROJECTS_DIR.exists():
        print(f"\n  projects: {len(list(PROJECTS_DIR.glob('*/current_state.json')))}")

    return 0 if ok0 and ok1 and ok2 and ok3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
