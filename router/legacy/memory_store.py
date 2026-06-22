"""Memory store: delta extraction, session state, and artifact indexing.

Architecture:
  Cursor full request → raw store (existing context_cache)
  → delta extractor (new messages since last request)
  → artifact store (tool results: raw + summary + index)
  → session state (current_state.json)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from artifact_analyzer import analyze_content, format_analysis_compact

from capture import _content_text, _sha256

LOG = logging.getLogger("router.memory")

_DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "tmp" / "context-cache"
CACHE_DIR = Path(os.getenv("CONTEXT_CACHE_DIR", str(_DEFAULT_CACHE)))
DELTA_DIR = CACHE_DIR / "deltas"
ARTIFACT_DIR = CACHE_DIR / "artifacts"
META_DIR = CACHE_DIR / "meta"
STATE_FILE = CACHE_DIR / "current_state.json"
PROJECTS_DIR = CACHE_DIR / "projects"
REGISTRY_FILE = PROJECTS_DIR / "_registry.json"

MEMORY_STORE_ENABLED = os.getenv("MEMORY_STORE", "1") == "1"
CURSOR_SUMMARY_MARKER = "[previous conversation summary]"
_lock = threading.Lock()


@dataclass
class DeltaMessage:
    index: int
    role: str
    chars: int
    fingerprint: str
    preview: str
    tool_name: str = ""
    tool_calls: list[str] = field(default_factory=list)
    file_refs: list[str] = field(default_factory=list)


@dataclass
class RequestDelta:
    delta_id: str
    req_id: str
    prev_req_id: str | None
    prev_message_count: int
    curr_message_count: int
    added_count: int
    added: list[DeltaMessage] = field(default_factory=list)
    has_new_user: bool = False
    last_role: str = ""
    diff_mode: str = ""


@dataclass
class Artifact:
    artifact_id: str
    req_id: str
    delta_id: str
    type: str  # tool_result | file_read | shell_result
    name: str
    command: str = ""
    path: str = ""
    raw_path: str = ""
    chars: int = 0
    summary: str = ""  # index/UI blurb only — not for LLM prompts
    prompt_excerpt: str = ""
    excerpt_chunks: list[str] = field(default_factory=list)
    index_terms: list[str] = field(default_factory=list)
    is_error: bool = False
    analysis: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str = ""
    chat_id: str = ""


@dataclass
class SessionState:
    session_id: str = ""
    updated_at: str = ""
    last_req_id: str = ""
    last_message_count: int = 0
    total_requests: int = 0
    current_query: str = ""
    phase_hint: str = ""
    files_read: list[str] = field(default_factory=list)
    read_counts: dict[str, int] = field(default_factory=dict)
    evidence_clusters: dict[str, Any] = field(default_factory=dict)
    read_avoidance_stats: dict[str, int] = field(default_factory=dict)
    file_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    commands_run: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    message_fingerprints: list[str] = field(default_factory=list)
    # Project / chat scoping (workspace path = project boundary)
    workspace_path: str = ""
    project_key: str = ""
    chat_id: str = ""
    session_reason: str = ""  # continue | new_project | new_chat | cursor_summary | count_shrink
    agent_plan: dict[str, Any] = field(default_factory=dict)
    failed_actions: dict[str, int] = field(default_factory=dict)
    steps_since_evidence: int = 0
    glob_unproductive: int = 0
    effective_workspace: str = ""
    previous_plan_failures: list[str] = field(default_factory=list)
    last_run_id: str = ""
    turn_index: int = 0
    # Loop guard (per user turn)
    final_answer_count: int = 0
    xml_leak_count: int = 0
    final_without_evidence_count: int = 0
    turns_since_progress: int = 0
    same_action_repeated: int = 0
    last_action_sig: str = ""
    active_query_hash: str = ""
    last_static_eval: dict[str, Any] = field(default_factory=dict)
    last_judge_decision: dict[str, Any] = field(default_factory=dict)
    judge_at: str = ""
    # Evidence judge / explore batch
    evidence_items: list[dict[str, Any]] = field(default_factory=list)
    explore_round: int = 0
    judge_round: int = 0
    tools_since_judge: int = 0
    missing_evidence: list[str] = field(default_factory=list)
    required_evidence_types: list[str] = field(default_factory=list)
    # Incremental message index (canonical)
    message_keys: list[str] = field(default_factory=list)
    context_index_snapshot: dict[str, Any] = field(default_factory=dict)
    phase_state: dict[str, Any] = field(default_factory=dict)
    last_ingest_metrics: dict[str, Any] = field(default_factory=dict)
    failed_tool_summaries: list[dict[str, Any]] = field(default_factory=list)
    last_prompt_sources: dict[str, str] = field(default_factory=dict)
    last_runtime_turn: dict[str, Any] = field(default_factory=dict)
    last_memory_hierarchy: dict[str, Any] = field(default_factory=dict)
    last_raw_tokens: int = 0
    runtime_state: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectPaths:
    project_key: str
    base: Path
    state_file: Path
    delta_dir: Path
    meta_dir: Path
    artifact_dir: Path


def _msg_fingerprint(msg: dict[str, Any]) -> str:
    """Backward-compatible short fingerprint — delegates to message_index."""
    from message_index import stable_message_key_short

    return stable_message_key_short(msg)


def _extract_file_refs(text: str) -> list[str]:
    return sorted(
        set(
            re.findall(
                r"([\w./~-]+\.(?:py|sh|yml|yaml|json|md|txt|gguf|html|pdf))",
                text,
                re.I,
            )
        )
    )[:10]


def normalize_file_path(path: str, workspace: str = "") -> str:
    """Normalize relative/basename paths for files_read matching."""
    p = (path or "").strip().replace("\\", "/")
    if not p:
        return ""
    if workspace and not p.startswith("/"):
        ws = workspace.rstrip("/")
        if p.startswith("./"):
            p = f"{ws}/{p[2:]}"
        elif not p.startswith("~/"):
            p = f"{ws}/{p.lstrip('/')}"
    try:
        return str(Path(p).expanduser().resolve())
    except (OSError, RuntimeError):
        return p


def _parse_tool_call_args(args_raw: Any) -> dict[str, Any]:
    if isinstance(args_raw, dict):
        return dict(args_raw)
    if not args_raw:
        return {}
    try:
        parsed = json.loads(args_raw) if isinstance(args_raw, str) else {}
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        out: dict[str, Any] = {}
        for key in ("path", "target_directory", "_source_id", "source_id", "glob_pattern", "pattern"):
            m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', str(args_raw))
            if m:
                out[key] = m.group(1)
        return out


def _resolve_tool_call_args(
    msg: dict[str, Any],
    delta: RequestDelta,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve assistant tool_call args for a tool result (Read/Glob/Grep)."""
    tc_id = str(msg.get("tool_call_id") or "")

    if tc_id and messages:
        for m in messages:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict) or str(tc.get("id") or "") != tc_id:
                    continue
                fn = tc.get("function", {})
                if not isinstance(fn, dict):
                    continue
                return _parse_tool_call_args(fn.get("arguments", ""))

    if msg.get("role") == "tool" and messages:
        idx = next((i for i, m in enumerate(messages) if m is msg), -1)
        if idx > 0:
            prev = messages[idx - 1]
            if isinstance(prev, dict) and prev.get("role") == "assistant":
                for tc in prev.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function", {})
                    if not isinstance(fn, dict):
                        continue
                    name = str(fn.get("name") or "")
                    if name in ("Read", "Glob", "Grep", "ReadSource", "GlobSource", "GrepSource"):
                        return _parse_tool_call_args(fn.get("arguments", ""))

    for d in reversed(delta.added):
        if d.role != "assistant" or not d.tool_calls:
            continue
        for tc in d.tool_calls:
            if any(t in tc for t in ("Glob", "Grep", "Read")):
                m = re.search(r"\{.*\}", tc)
                if m:
                    return _parse_tool_call_args(m.group(0))
    return {}


def _glob_result_directory(text: str) -> str:
    m = re.search(r"Result of search in '([^']+)'", text or "")
    if m:
        return m.group(1).strip()
    return ""


def _resolve_read_path(
    msg: dict[str, Any],
    delta: RequestDelta,
    text: str,
    messages: list[dict[str, Any]] | None = None,
) -> str:
    """Resolve Read/Glob/Grep target path from assistant tool_call or content hints."""
    tc_args = _resolve_tool_call_args(msg, delta, messages=messages)
    target_dir = str(tc_args.get("target_directory") or "").strip()
    if target_dir:
        return target_dir

    tc_id = str(msg.get("tool_call_id") or "")

    if tc_id and messages:
        for m in messages:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict) or str(tc.get("id") or "") != tc_id:
                    continue
                fn = tc.get("function", {})
                if not isinstance(fn, dict):
                    continue
                args_raw = fn.get("arguments", "")
                if isinstance(args_raw, dict):
                    path = args_raw.get("path")
                    if path:
                        return str(path)
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else {}
                    if isinstance(args, dict) and args.get("path"):
                        return str(args["path"])
                except json.JSONDecodeError:
                    m_path = re.search(r'"path"\s*:\s*"([^"]+)"', str(args_raw))
                    if m_path:
                        return m_path.group(1)

    for d in reversed(delta.added):
        if d.role != "assistant" or not d.tool_calls:
            continue
        for tc in d.tool_calls:
            m = re.search(r'Read\(\{.*?"path"\s*:\s*"([^"]+)"', tc)
            if m:
                return m.group(1)
            m = re.search(r'"path"\s*:\s*"([^"]+)"', tc)
            if m and "Read" in tc:
                return m.group(1)
    if msg.get("role") == "tool" and messages:
        idx = next(
            (i for i, m in enumerate(messages) if m is msg),
            -1,
        )
        if idx > 0:
            prev = messages[idx - 1]
            if isinstance(prev, dict) and prev.get("role") == "assistant":
                for tc in prev.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function", {})
                    if not isinstance(fn, dict) or fn.get("name") != "Read":
                        continue
                    args_raw = fn.get("arguments", "")
                    try:
                        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                        if isinstance(args, dict) and args.get("path"):
                            return str(args["path"])
                    except json.JSONDecodeError:
                        m_path = re.search(r'"path"\s*:\s*"([^"]+)"', str(args_raw))
                        if m_path:
                            return m_path.group(1)
    m = re.search(r"# cached content for (.+)", text)
    if m:
        return m.group(1).strip()
    glob_dir = _glob_result_directory(text)
    if glob_dir:
        return glob_dir
    refs = _extract_file_refs(text)
    return refs[0] if refs else ""


def _summarize_tool_content(text: str, max_len: int = 200) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "(empty)"
    # Prefer first meaningful line
    for ln in lines[:8]:
        if ln.startswith("Error:") or ln.startswith("Exit code:"):
            return ln[:max_len]
        if "|" in ln and len(ln) < 120:  # table row
            return ln[:max_len]
        if ln.endswith((".py", ".yml", ".json", ".sh")):
            return f"file: {ln[:80]}"
    return lines[0][:max_len]


def _index_terms_for_tool(name: str, text: str, tool_calls: list[str]) -> list[str]:
    terms: list[str] = [name] + tool_calls
    terms.extend(_extract_file_refs(text))
    # docker/shell keywords
    for kw in ["docker ps", "docker logs", "router/status", "route_backend", "docker-compose"]:
        if kw in text.lower() or kw in text:
            terms.append(kw)
    # command from exit code block
    m = re.search(r"Command output:\s*\n+```\n(.+?)\n```", text, re.S)
    if m:
        first_line = m.group(1).strip().splitlines()[0][:60]
        if first_line:
            terms.append(first_line)
    return list(dict.fromkeys(t for t in terms if t))[:15]


def _delta_message_from_dict(msg: dict[str, Any], index: int) -> DeltaMessage:
    role = str(msg.get("role", "?"))
    text = _content_text(msg.get("content", ""))
    tc_list: list[str] = []
    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            if isinstance(tc, dict):
                fn = tc.get("function", {})
                if isinstance(fn, dict):
                    n = str(fn.get("name", ""))
                    args = str(fn.get("arguments", ""))[:80]
                    tc_list.append(f"{n}({args})")
    return DeltaMessage(
        index=index,
        role=role,
        chars=len(text),
        fingerprint=_msg_fingerprint(msg),
        preview=text[:120].replace("\n", " "),
        tool_name=str(msg.get("name", "")),
        tool_calls=tc_list,
        file_refs=_extract_file_refs(text),
    )


def extract_workspace_path(body: dict[str, Any]) -> str:
    """Extract Cursor workspace path from user_info message; resolve to repo root."""
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return ""
    raw = ""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _content_text(msg.get("content", ""))
        if "Workspace Path:" not in text:
            continue
        m = re.search(r"Workspace Path:\s*(\S+)", text)
        if m:
            raw = m.group(1).rstrip("/")
            break
    if not raw:
        return ""
    try:
        from reference.project_root import effective_workspace

        return effective_workspace(raw)
    except ImportError:
        return raw


def resolve_session_workspace(body: dict[str, Any], state: "SessionState") -> str:
    """Resolve workspace for planner/registry — never persist container /app as host root."""
    try:
        from reference.project_root import effective_workspace, is_container_router_path
    except ImportError:
        return extract_workspace_path(body) or state.workspace_path or ""

    ws = extract_workspace_path(body) or state.workspace_path or ""
    if is_container_router_path(ws):
        snap = getattr(state, "context_index_snapshot", None) or {}
        alt = str(snap.get("workspace_path") or "")
        if alt and not is_container_router_path(alt):
            ws = effective_workspace(alt)
        else:
            ws = effective_workspace("", list(state.files_read[-12:]))
    elif ws:
        ws = effective_workspace(ws, list(state.files_read[-12:]))
    return ws


def project_key_from_workspace(workspace: str) -> str:
    if not workspace:
        return "unknown"
    return _sha256(workspace)[:12]


def project_paths(project_key: str) -> ProjectPaths:
    base = PROJECTS_DIR / project_key
    return ProjectPaths(
        project_key=project_key,
        base=base,
        state_file=base / "current_state.json",
        delta_dir=base / "deltas",
        meta_dir=base / "meta",
        artifact_dir=base / "artifacts",
    )


def is_fresh_chat(messages: list[Any]) -> bool:
    """True when Cursor sent a brand-new chat (no assistant/tool history yet)."""
    roles = {str(m.get("role", "")) for m in messages if isinstance(m, dict)}
    return "assistant" not in roles and "tool" not in roles


def detect_cursor_summary(body: dict[str, Any]) -> bool:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _content_text(msg.get("content", "")).lower()
        if CURSOR_SUMMARY_MARKER in text:
            return True
    return False


def _new_chat_id() -> str:
    return _sha256(str(time.time()))[:10]


def _query_fingerprint(query: str) -> str:
    return _sha256((query or "").strip()[:500])[:16]


def _had_chat_history(state: SessionState) -> bool:
    return state.last_message_count > 0 and bool(
        state.artifacts
        or state.files_read
        or state.commands_run
        or (state.agent_plan or {}).get("goal")
        or state.turn_index > 0
        or state.evidence_items
        or state.turns_since_progress > 0
    )


def _blank_chat_state(
    workspace: str,
    project_key: str,
    reason: str,
    *,
    prev: SessionState | None = None,
    rebaseline_messages: list[Any] | None = None,
) -> SessionState:
    """Full chat-scoped reset — artifacts, plan, loop counters, phase state."""
    total = int(getattr(prev, "total_requests", 0) or 0) if prev else 0
    st = SessionState(
        session_id=_sha256(f"{project_key}:{time.time()}")[:12],
        chat_id=_new_chat_id(),
        workspace_path=workspace,
        project_key=project_key,
        session_reason=reason,
        total_requests=total,
    )
    if rebaseline_messages is not None:
        st.last_message_count = len(rebaseline_messages)
        st.message_fingerprints = [
            _msg_fingerprint(m) for m in rebaseline_messages if isinstance(m, dict)
        ]
        try:
            from message_index import stable_message_key

            st.message_keys = [
                stable_message_key(m) for m in rebaseline_messages if isinstance(m, dict)
            ]
        except ImportError:
            pass
    try:
        from reference.loop_guard import reset_loop_counters

        reset_loop_counters(st, "")
    except ImportError:
        pass
    cleared = len(getattr(prev, "artifacts", None) or []) if prev else 0
    LOG.info(
        "memory chat reset reason=%s project=%s chat_id=%s cleared_artifacts=%d prev_msgs=%d",
        reason,
        project_key,
        st.chat_id,
        cleared,
        int(getattr(prev, "last_message_count", 0) or 0),
    )
    return st


def _fresh_project_state(workspace: str, project_key: str, reason: str) -> SessionState:
    return _blank_chat_state(workspace, project_key, reason)


def rebaseline_state(
    state: SessionState,
    messages: list[Any],
    reason: str,
) -> SessionState:
    """Cursor summary / rebaseline — reset chat memory, keep project boundary only."""
    return _blank_chat_state(
        state.workspace_path,
        state.project_key,
        reason,
        prev=state,
        rebaseline_messages=messages if isinstance(messages, list) else [],
    )


def resolve_session(
    body: dict[str, Any],
    state: SessionState,
    workspace: str,
    project_key: str,
) -> tuple[SessionState, str]:
    """Decide whether to continue, start a new chat, or switch project."""
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    if (
        state.project_key
        and project_key != "unknown"
        and state.project_key != project_key
    ):
        LOG.info(
            "memory project switch %s(%s) -> %s(%s)",
            state.project_key,
            state.workspace_path,
            project_key,
            workspace,
        )
        return _fresh_project_state(workspace, project_key, "new_project"), "new_project"

    state.workspace_path = workspace or state.workspace_path
    state.project_key = project_key if project_key != "unknown" else state.project_key

    n = len(messages)
    prev_n = state.last_message_count
    had_history = _had_chat_history(state)

    # Cursor new composer: body shrinks sharply (e.g. 138 → 3).
    if prev_n >= 15 and n <= max(8, prev_n // 4) and had_history:
        return _blank_chat_state(workspace, project_key, "count_shrink", prev=state), "new_chat"

    fresh = is_fresh_chat(messages)

    if fresh and had_history and n <= max(prev_n, 10):
        LOG.info(
            "memory new chat in project %s workspace=%s prev_msgs=%d curr=%d",
            project_key,
            workspace,
            prev_n,
            n,
        )
        return _blank_chat_state(workspace, project_key, "new_chat", prev=state), "new_chat"

    if fresh and not state.chat_id:
        return _blank_chat_state(workspace, project_key, "new_chat"), "new_chat"

    if not state.chat_id:
        state.chat_id = _new_chat_id()

    state.session_reason = state.session_reason or "continue"
    return state, "continue"


def _active_project_key() -> str:
    if not REGISTRY_FILE.exists():
        return ""
    try:
        registry = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
        return str(registry.get("active_project", "") or "")
    except (json.JSONDecodeError, TypeError):
        return ""


def load_active_state() -> SessionState:
    pk = _active_project_key()
    if pk:
        return load_state(pk)
    return load_state()


def _update_registry(project_key: str, workspace: str) -> None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    registry: dict[str, Any] = {"active_project": project_key, "projects": {}}
    if REGISTRY_FILE.exists():
        try:
            registry = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError):
            pass
    projects = registry.setdefault("projects", {})
    projects[project_key] = {
        "workspace": workspace,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    registry["active_project"] = project_key
    REGISTRY_FILE.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_state(project_key: str | None = None) -> SessionState:
    paths: list[Path] = []
    if project_key and project_key != "unknown":
        paths.append(project_paths(project_key).state_file)
    paths.append(STATE_FILE)
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            fields = SessionState.__dataclass_fields__
            return SessionState(**{k: data[k] for k in fields if k in data})
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    seed = project_key or _sha256(str(time.time()))[:12]
    return SessionState(session_id=_sha256(seed)[:12])


def save_state(state: SessionState, project_key: str | None = None) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    state.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = json.dumps(asdict(state), ensure_ascii=False, indent=2)
    pk = project_key or state.project_key
    if pk and pk != "unknown":
        pdir = project_paths(pk)
        pdir.base.mkdir(parents=True, exist_ok=True)
        pdir.state_file.write_text(payload, encoding="utf-8")
        _update_registry(pk, state.effective_workspace or state.workspace_path or workspace)
    STATE_FILE.write_text(payload, encoding="utf-8")


def _last_user_query_index(messages: list[Any]) -> int:
    idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            text = _content_text(msg.get("content", ""))
            if "<user_query>" in text:
                idx = i
    return idx


def extract_delta(
    req_id: str,
    body: dict[str, Any],
    prev_state: SessionState | None = None,
) -> RequestDelta:
    """Compare current request with previous state; return only new messages."""
    from message_index import diff_messages, log_ingest_metrics, stable_message_key

    messages = body.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    prev_state = prev_state or load_state()
    prev_count = prev_state.last_message_count
    prev_req_id = prev_state.last_req_id or None
    prev_keys = list(prev_state.message_keys or [])

    count_shrink = False
    shrink_start = 0

    if prev_count > 0 and len(messages) < prev_count:
        LOG.info(
            "memory count shrink req=%s prev_count=%d curr=%d project=%s",
            req_id,
            prev_count,
            len(messages),
            prev_state.project_key,
        )
        count_shrink = True
        last_uq = _last_user_query_index(messages)
        shrink_start = last_uq if last_uq >= 0 else max(0, len(messages) - 1)
        prev_count = shrink_start
        if prev_state.session_reason not in ("new_project", "new_chat", "cursor_summary"):
            prev_state.session_reason = "count_shrink"
        if not prev_keys and prev_state.message_fingerprints:
            prev_keys = [stable_message_key(m) for m in messages[:shrink_start] if isinstance(m, dict)]
        else:
            prev_keys = [stable_message_key(m) for m in messages[:shrink_start] if isinstance(m, dict)]

    if not prev_keys and prev_count > 0 and not count_shrink:
        prev_keys = [stable_message_key(m) for m in messages[:prev_count] if isinstance(m, dict)]

    diff = diff_messages(
        messages,
        prev_keys,
        count_shrink=count_shrink,
        shrink_start=shrink_start if count_shrink else prev_count,
    )

    if diff.mode == "rebuild" and prev_keys:
        LOG.warning(
            "memory delta key mismatch req=%s prev_keys=%d all_keys=%d — rebuild tail",
            req_id,
            len(prev_keys),
            len(diff.all_keys),
        )

    added: list[DeltaMessage] = []
    has_new_user = False
    for idx, msg in diff.new_messages:
        if not isinstance(msg, dict):
            continue
        dm = _delta_message_from_dict(msg, idx)
        added.append(dm)
        if dm.role == "user" and "<user_query>" in _content_text(msg.get("content", "")):
            has_new_user = True

    prev_state.message_keys = list(diff.all_keys)
    prev_state.message_fingerprints = [_msg_fingerprint(m) for m in messages if isinstance(m, dict)]

    metrics = log_ingest_metrics(
        req_id,
        diff,
        plan_input_mode="pending",
        phase_update_mode="pending",
    )
    prev_state.last_ingest_metrics = metrics

    delta_id = f"{req_id}_delta"
    return RequestDelta(
        delta_id=delta_id,
        req_id=req_id,
        prev_req_id=prev_req_id,
        prev_message_count=prev_count,
        curr_message_count=len(messages),
        added_count=len(added),
        added=added,
        has_new_user=has_new_user,
        last_role=added[-1].role if added else (messages[-1].get("role", "") if messages else ""),
        diff_mode=diff.mode,
    )


def _find_added_by_fingerprint(prev_fps: list[str], messages: list) -> list[dict]:
    """Fallback: find messages after last matching fingerprint."""
    if not prev_fps:
        return [m for m in messages if isinstance(m, dict)]
    curr_fps = [_msg_fingerprint(m) for m in messages if isinstance(m, dict)]
    # Find longest common prefix
    common = 0
    for i, fp in enumerate(prev_fps):
        if i < len(curr_fps) and curr_fps[i] == fp:
            common = i + 1
        else:
            break
    return [m for m in messages[common:] if isinstance(m, dict)]


def _save_artifact(
    req_id: str,
    delta: RequestDelta,
    msg: dict[str, Any],
    dm: DeltaMessage,
    messages: list[dict[str, Any]] | None = None,
    state: SessionState | None = None,
) -> Artifact | None:
    text = _content_text(msg.get("content", ""))
    role = dm.role

    if role == "tool":
        name = dm.tool_name or ""
        art_type = "shell_result" if "Exit code:" in text else "tool_result"
        command = ""
        path = _resolve_read_path(msg, delta, text, messages)
        m = re.search(r"Command output:", text)
        if m:
            art_type = "shell_result"
            # try to infer command from preceding assistant tool_call in delta
            for d in delta.added:
                if d.role == "assistant" and d.tool_calls:
                    for tc in d.tool_calls:
                        if "Shell" in tc:
                            command = tc
                            break
        if not name:
            for d in reversed(delta.added):
                if d.role == "assistant" and d.tool_calls:
                    for tc in d.tool_calls:
                        if "Read" in tc:
                            name = "Read"
                            break
                    if name:
                        break
        if name == "Read" or path or text.strip().startswith(("1|", "```", "# cached content for")):
            art_type = "file_read"
            if not path and dm.file_refs:
                path = dm.file_refs[0]

        failure = None
        try:
            from failed_action import detect_tool_failure, record_failed_tool

            ws = (state.workspace_path if state else "") or ""
            failure = detect_tool_failure(name or art_type, path, text, workspace=ws)
            if failure and state is not None:
                record_failed_tool(state, failure)
        except ImportError:
            pass

        if failure:
            # Policy constraint only — not an evidence artifact.
            return None

        artifact_id = f"{req_id}_{dm.index}"
        raw_path = ARTIFACT_DIR / f"{artifact_id}.txt"
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(text, encoding="utf-8")

        from artifact_excerpt import build_prompt_excerpt

        analysis: dict[str, Any] = {}
        summary = ""
        prompt_excerpt = ""
        excerpt_chunks: list[str] = []
        if text.strip():
            prompt_excerpt, excerpt_chunks = build_prompt_excerpt(
                text,
                path=path,
                tool_name=name,
                art_type=art_type,
            )
            is_grep_blob = "<workspace_result" in text[:800] or name.lower() == "grep"
            if is_grep_blob and len(text) >= 800:
                from artifact_excerpt import (
                    ARTIFACT_LLM_FINAL_MIN_CHUNK_CHARS,
                    _merge_chunks,
                    apply_llm_one_pass,
                    DEFAULT_INGEST_EXCERPT_CHARS,
                )

                excerpt_chunks = apply_llm_one_pass(
                    excerpt_chunks,
                    path=path,
                    raw_len=len(text),
                    kind="grep",
                    min_chunk_chars=ARTIFACT_LLM_FINAL_MIN_CHUNK_CHARS,
                    force=True,
                )
                prompt_excerpt = _merge_chunks(excerpt_chunks, max_chars=DEFAULT_INGEST_EXCERPT_CHARS)
        if art_type == "file_read" and text.strip():
            analysis = analyze_content(text, path=path, tool_name=name)
            summary = format_analysis_compact(analysis)  # index blurb only
        elif art_type == "shell_result" and text.strip():
            analysis = analyze_content(text, path=path, tool_name=name or "Shell")
            summary = format_analysis_compact(analysis) if analysis.get("kind") != "shell" else _summarize_tool_content(text)
            if analysis.get("kind") == "html_validation":
                summary = format_analysis_compact(analysis)
        else:
            summary = _summarize_tool_content(text) if not prompt_excerpt else f"[indexed {art_type} chars={len(text)}]"
        terms = _index_terms_for_tool(name, text, dm.tool_calls)

        return Artifact(
            artifact_id=artifact_id,
            req_id=req_id,
            delta_id=delta.delta_id,
            type=art_type,
            name=name,
            command=command,
            path=path,
            raw_path=str(raw_path),
            chars=len(text),
            summary=summary,
            prompt_excerpt=prompt_excerpt,
            excerpt_chunks=excerpt_chunks,
            index_terms=terms,
            is_error=text.strip().startswith("Error:") or "Traceback" in text[:300],
            analysis=analysis,
            tool_call_id=str(msg.get("tool_call_id") or ""),
            chat_id=(state.chat_id if state else "") or "",
        )

    if role == "assistant" and dm.tool_calls:
        artifact_id = f"{req_id}_{dm.index}_call"
        summary = "; ".join(dm.tool_calls)
        terms = []
        for tc in dm.tool_calls:
            terms.extend(re.findall(r'"path"\s*:\s*"([^"]+)"', tc))
            terms.extend(re.findall(r'"command"\s*:\s*"([^"]+)"', tc))
        return Artifact(
            artifact_id=artifact_id,
            req_id=req_id,
            delta_id=delta.delta_id,
            type="tool_call",
            name=dm.tool_calls[0].split("(")[0] if dm.tool_calls else "unknown",
            summary=summary,
            index_terms=list(dict.fromkeys(terms))[:10],
            chat_id=(state.chat_id if state else "") or "",
        )

    return None


def _artifact_raw_text(art: Artifact, max_chars: int = 120_000) -> str:
    """Load full tool result text for evidence extraction (not compact summary)."""
    raw_path = (art.raw_path or "").strip()
    if raw_path:
        try:
            p = Path(raw_path)
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                return text[:max_chars]
        except OSError:
            pass
    return ""


def _tool_success_from_text(art: Artifact, text: str) -> bool:
    if art.is_error:
        return False
    if art.type == "shell_result":
        return "Exit code: 0" in text or "Exit code: 1" not in text
    return bool(text.strip())


def _ingest_tool_artifacts_for_plan(
    state: SessionState,
    artifacts: list[Artifact],
    *,
    run_id: str = "",
    messages: list[dict[str, Any]] | None = None,
    delta: RequestDelta | None = None,
) -> None:
    """Raw tool result → evidence_collected + agent_runs events."""
    if not artifacts:
        return
    try:
        from legacy.agent_runs import current_run_id, emit_tool_call
        from reference.planner import AgentPlan, update_plan_after_tool

        rid = current_run_id() or run_id
        if not state.agent_plan:
            return
        ap = AgentPlan.from_dict(state.agent_plan)
        empty_delta = delta or RequestDelta(
            delta_id="",
            req_id=run_id,
            prev_req_id=None,
            prev_message_count=0,
            curr_message_count=0,
            added_count=0,
        )

        for art in artifacts:
            if art.type not in ("file_read", "shell_result", "tool_result"):
                continue
            tool = art.name or ("Read" if art.type == "file_read" else "Shell")
            args: dict[str, Any] = {}
            tc_args = _resolve_tool_call_args(
                {"tool_call_id": art.tool_call_id},
                empty_delta,
                messages=messages,
            )
            if art.path:
                args["path"] = art.path
            if art.command:
                args["command"] = art.command
            if tc_args.get("target_directory"):
                args["target_directory"] = tc_args["target_directory"]
            if tc_args.get("glob_pattern"):
                args["glob_pattern"] = tc_args["glob_pattern"]
            if tc_args.get("pattern"):
                args["pattern"] = tc_args["pattern"]
            if tc_args.get("query"):
                args["query"] = tc_args["query"]
            if tc_args.get("_source_id"):
                args["_source_id"] = tc_args["_source_id"]
            if tc_args.get("source_id"):
                args["source_id"] = tc_args["source_id"]
            if ap.source_registry:
                try:
                    from reference.source_registry import (
                        SourceRegistry,
                        lookup_source_id_by_relpath,
                        resolve_path_via_registry,
                    )

                    reg = SourceRegistry.from_dict(ap.source_registry)
                    sid = str(tc_args.get("_source_id") or tc_args.get("source_id") or "").strip()
                    if not sid and tc_args.get("target_directory"):
                        sid = lookup_source_id_by_relpath(reg, str(tc_args["target_directory"])) or ""
                    if not sid and art.path:
                        sid = lookup_source_id_by_relpath(reg, art.path) or ""
                    if not sid and art.path:
                        try:
                            _, sid = resolve_path_via_registry(reg, art.path)
                        except (KeyError, ValueError, TypeError):
                            sid = ""
                    if sid:
                        args["_source_id"] = sid
                        args["source_id"] = sid
                except (KeyError, ValueError, TypeError):
                    pass
            raw_text = _artifact_raw_text(art)
            success = _tool_success_from_text(art, raw_text)
            ap = update_plan_after_tool(
                ap,
                state,
                tool_name=tool,
                args=args,
                result_text=raw_text,
                success=success,
                emit_run_events=bool(rid),
                run_id=rid,
            )
            if rid and success:
                emit_tool_call(
                    rid,
                    call_id=f"ingest_{art.artifact_id}",
                    name=tool,
                    status="completed",
                    args=args,
                    result=raw_text[:400],
                )
        state.agent_plan = ap.to_dict()
    except Exception as exc:
        LOG.warning("ingest tool evidence failed: %s", exc)


def update_state_from_delta(
    state: SessionState,
    req_id: str,
    body: dict[str, Any],
    delta: RequestDelta,
    artifacts: list[Artifact],
    query: str = "",
) -> SessionState:
    messages = body.get("messages", [])
    state.last_req_id = req_id
    state.last_message_count = len(messages) if isinstance(messages, list) else 0
    state.total_requests += 1
    state.message_fingerprints = [
        _msg_fingerprint(m) for m in messages if isinstance(m, dict)
    ]

    if query:
        state.current_query = query
    elif delta.has_new_user:
        from context_cache import extract_last_user_query

        state.current_query = extract_last_user_query(body)
        try:
            from reference.loop_guard import reset_loop_counters

            reset_loop_counters(state, state.current_query)
        except ImportError:
            state.final_answer_count = 0
            state.xml_leak_count = 0

    workspace = resolve_session_workspace(body, state)

    for art in artifacts:
        if art.artifact_id not in state.artifacts:
            state.artifacts.append(art.artifact_id)
        if art.type == "file_read" and art.path:
            norm = normalize_file_path(art.path, workspace)
            key = norm or art.path
            if norm and norm not in state.files_read:
                state.files_read.append(norm)
            elif art.path not in state.files_read:
                state.files_read.append(art.path)
            try:
                from runtime_core.evidence_cluster import record_artifact_access

                cluster_id, redundant = record_artifact_access(state, art, workspace=workspace)
                if not redundant:
                    state.read_counts[cluster_id] = state.read_counts.get(cluster_id, 0) + 1
            except ImportError:
                state.read_counts[key] = state.read_counts.get(key, 0) + 1
            if art.analysis:
                state.file_meta[key] = art.analysis
        if art.type == "shell_result" and art.analysis:
            key = art.path or art.summary[:80]
            if art.analysis:
                state.file_meta[key] = art.analysis
            cmd = art.index_terms[0] if art.index_terms else art.summary
            if cmd not in state.commands_run:
                state.commands_run.append(cmd)

    # Phase hint from plan state
    try:
        from reference.plan_state import compute_plan_phase_hint

        q = query or state.current_query
        state.phase_hint = compute_plan_phase_hint(state, q, artifacts)
    except Exception:
        if delta.last_role == "tool":
            state.phase_hint = "tool_planning"
        elif delta.has_new_user:
            state.phase_hint = "new_user_turn"

    q = query or state.current_query
    if q:
        try:
            from reference.planner import ensure_agent_plan

            force_replan = state.session_reason in (
                "new_chat",
                "new_project",
                "count_shrink",
                "new_query",
                "loop_escape",
                "cursor_summary",
            )
            ensure_agent_plan(state, q, force_replan=force_replan)
        except Exception:
            pass

    _ingest_tool_artifacts_for_plan(
        state, artifacts, run_id=req_id, messages=messages if isinstance(messages, list) else None, delta=delta,
    )

    try:
        from reference.loop_guard import record_turn_progress, snapshot_progress

        after = snapshot_progress(state)
        before_count = int(getattr(state, "_progress_snapshot_evidence", after.evidence_count))
        before_files = int(getattr(state, "_progress_snapshot_files", after.files_read_count))
        before_step = int(getattr(state, "_progress_snapshot_step", after.plan_step))
        from reference.loop_guard import ProgressSnapshot

        before = ProgressSnapshot(
            evidence_count=before_count,
            artifact_count=max(0, after.artifact_count - len(artifacts)),
            files_read_count=before_files,
            plan_step=before_step,
        )
        record_turn_progress(state, before, after)
        state._progress_snapshot_evidence = after.evidence_count
        state._progress_snapshot_files = after.files_read_count
        state._progress_snapshot_step = after.plan_step
    except Exception:
        pass

    try:
        from legacy.agent_runs import current_run_id, link_run_chain

        link_run_chain(current_run_id() or req_id, state)
    except Exception:
        pass

    return state


def ingest_request(
    req_id: str,
    body: dict[str, Any],
    query: str = "",
) -> tuple[RequestDelta, SessionState, list[Artifact]]:
    """Full intake pipeline: delta → artifacts → state update → persist."""
    if not MEMORY_STORE_ENABLED:
        empty_delta = RequestDelta(
            delta_id=f"{req_id}_delta",
            req_id=req_id,
            prev_req_id=None,
            prev_message_count=0,
            curr_message_count=0,
            added_count=0,
        )
        return empty_delta, load_state(), []

    with _lock:
        workspace = extract_workspace_path(body)
        project_key = project_key_from_workspace(workspace)
        active_pk = _active_project_key()

        if active_pk and project_key != "unknown" and active_pk != project_key:
            LOG.info(
                "memory project switch active=%s -> %s workspace=%s",
                active_pk,
                project_key,
                workspace,
            )
            state = _fresh_project_state(workspace, project_key, "new_project")
            transition = "new_project"
            pre_reset = state
        else:
            state = load_state(project_key if project_key != "unknown" else None)
            pre_reset = SessionState(**{k: getattr(state, k) for k in SessionState.__dataclass_fields__})
            state, transition = resolve_session(body, state, workspace, project_key)
        state.session_reason = transition

        messages = body.get("messages", [])
        if not isinstance(messages, list):
            messages = []

        if detect_cursor_summary(body) and _had_chat_history(state):
            LOG.info("memory cursor summary reset project=%s msgs=%d", project_key, len(messages))
            state = rebaseline_state(state, messages, "cursor_summary")
            transition = "cursor_summary"
            state.session_reason = transition

        # Stuck loop: user re-sent same task — reset chat memory but keep message baseline.
        if transition == "continue" and messages:
            try:
                from context_cache import extract_last_user_query

                uq = query or extract_last_user_query(body)
            except ImportError:
                uq = query or ""
            qh = _query_fingerprint(uq)
            new_user_at_end = (
                len(messages) > pre_reset.last_message_count
                and isinstance(messages[-1], dict)
                and messages[-1].get("role") == "user"
                and "<user_query>" in _content_text(messages[-1].get("content", ""))
            )
            stuck = (
                new_user_at_end
                and pre_reset.turns_since_progress >= 6
                and len(pre_reset.artifacts) >= 8
                and qh
                and qh == pre_reset.active_query_hash
            )
            if stuck:
                LOG.info(
                    "memory loop escape project=%s turns_stuck=%d artifacts=%d",
                    project_key,
                    pre_reset.turns_since_progress,
                    len(pre_reset.artifacts),
                )
                state = _blank_chat_state(
                    workspace,
                    project_key,
                    "loop_escape",
                    prev=pre_reset,
                    rebaseline_messages=messages[:-1],
                )
                transition = "new_chat"
                state.session_reason = transition
                if uq:
                    try:
                        from reference.loop_guard import reset_loop_counters

                        reset_loop_counters(state, uq)
                    except ImportError:
                        state.active_query_hash = qh

        pdirs = (
            project_paths(project_key)
            if project_key != "unknown"
            else ProjectPaths(
                project_key="unknown",
                base=CACHE_DIR,
                state_file=STATE_FILE,
                delta_dir=DELTA_DIR,
                meta_dir=META_DIR,
                artifact_dir=ARTIFACT_DIR,
            )
        )

        delta = extract_delta(req_id, body, state)

        if isinstance(messages, list) and delta.added:
            try:
                from message_index import index_message
                from reference.plan_state import apply_phase_events

                indexed_new = [
                    index_message(messages[dm.index], dm.index)
                    for dm in delta.added
                    if 0 <= dm.index < len(messages) and isinstance(messages[dm.index], dict)
                ]
                apply_phase_events(state, indexed_new)
                if state.last_ingest_metrics:
                    state.last_ingest_metrics["plan_input_mode"] = "snapshot"
                    state.last_ingest_metrics["phase_update_mode"] = "event"
            except Exception as exc:
                LOG.warning("phase event update failed: %s", exc)
        if isinstance(messages, list) and delta.added:
            added_msgs = [
                messages[dm.index]
                for dm in delta.added
                if 0 <= dm.index < len(messages) and isinstance(messages[dm.index], dict)
            ]
        else:
            added_msgs = []
        if (
            transition != "cursor_summary"
            and any(
                isinstance(m, dict)
                and m.get("role") == "user"
                and CURSOR_SUMMARY_MARKER in _content_text(m.get("content", "")).lower()
                for m in added_msgs
            )
        ):
            LOG.info("memory cursor summary rebaseline project=%s", project_key)
            state = rebaseline_state(state, messages, "cursor_summary")
            transition = "cursor_summary"
            added_msgs = []

        artifacts: list[Artifact] = []

        for i, msg in enumerate(added_msgs):
            if not isinstance(msg, dict):
                continue
            dm = delta.added[i] if i < len(delta.added) else _delta_message_from_dict(msg, delta.prev_message_count + i)
            art = _save_artifact(
                req_id, delta, msg, dm,
                messages if isinstance(messages, list) else None,
                state=state,
            )
            if art:
                artifacts.append(art)
                pdirs.artifact_dir.mkdir(parents=True, exist_ok=True)
                art_path = pdirs.artifact_dir / f"{art.artifact_id}.json"
                art_path.write_text(json.dumps(asdict(art), ensure_ascii=False, indent=2), encoding="utf-8")

        state = update_state_from_delta(state, req_id, body, delta, artifacts, query=query)
        save_state(state, project_key)

        pdirs.delta_dir.mkdir(parents=True, exist_ok=True)
        delta_path = pdirs.delta_dir / f"{delta.delta_id}.json"
        delta_path.write_text(
            json.dumps(
                {
                    **asdict(delta),
                    "artifacts": [a.artifact_id for a in artifacts],
                    "project_key": project_key,
                    "workspace_path": workspace,
                    "session_reason": transition,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        pdirs.meta_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "req_id": req_id,
            "delta_id": delta.delta_id,
            "prev_req_id": delta.prev_req_id,
            "message_count": delta.curr_message_count,
            "added_count": delta.added_count,
            "last_role": delta.last_role,
            "has_new_user": delta.has_new_user,
            "phase_hint": state.phase_hint,
            "artifacts": [a.artifact_id for a in artifacts],
            "project_key": project_key,
            "workspace_path": workspace,
            "chat_id": state.chat_id,
            "session_reason": transition,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (pdirs.meta_dir / f"{req_id}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        LOG.info(
            "memory req=%s project=%s chat=%s reason=%s delta=+%d msgs (%d→%d) last=%s artifacts=%d phase_hint=%s",
            req_id,
            project_key,
            state.chat_id,
            transition,
            delta.added_count,
            delta.prev_message_count,
            delta.curr_message_count,
            delta.last_role,
            len(artifacts),
            state.phase_hint,
        )
        for dm in delta.added:
            extra = f" tc={dm.tool_calls}" if dm.tool_calls else ""
            LOG.info(
                "  delta[%d] %s %dchars%s preview=%r",
                dm.index,
                dm.role,
                dm.chars,
                extra,
                dm.preview[:60],
            )

        return delta, state, artifacts
