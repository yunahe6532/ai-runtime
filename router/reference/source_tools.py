"""Runtime bridge: LLM source_id tools → Cursor path-based tools."""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

from .source_registry import (
    SOURCE_TOOL_NAMES,
    SourceRegistry,
    WIDE_DIR_GREP_PATTERN,
    expand_source_tool_call,
    llm_source_tool_definitions,
    lookup_source_id_by_relpath,
    resolve_path_via_registry,
)

LOG = logging.getLogger("router.source_tools")

LEGACY_PATH_TOOLS = frozenset({"Read", "Grep", "Glob"})


def registry_from_plan(plan: Any) -> SourceRegistry | None:
    raw = None
    if hasattr(plan, "source_registry"):
        raw = plan.source_registry
    elif isinstance(plan, dict):
        raw = plan.get("source_registry")
    if not raw:
        return None
    reg = SourceRegistry.from_dict(raw if isinstance(raw, dict) else {})
    if reg.available():
        return reg
    return None


def _refresh_registry_on_plan(plan: Any) -> SourceRegistry | None:
    """Rebuild registry when persisted entries are stale (e.g. container layout drift)."""
    from .source_registry import build_source_registry, resolve_root_mapping

    preferred: list[str] = []
    known: list[str] = []
    prev_host = ""
    if hasattr(plan, "preferred_sources"):
        preferred = list(plan.preferred_sources or [])
        known = list(getattr(plan, "known_files", None) or [])
        prev_host = str((getattr(plan, "source_registry", None) or {}).get("root", {}).get("host") or "")
    elif isinstance(plan, dict):
        preferred = list(plan.get("preferred_sources") or [])
        known = list(plan.get("known_files") or [])
        prev_host = str((plan.get("source_registry") or {}).get("root", {}).get("host") or "")

    if not preferred:
        return None

    from .project_root import effective_workspace, is_container_router_path

    ws = prev_host if prev_host and not is_container_router_path(prev_host) else ""
    root = effective_workspace(ws, known)
    mapping = resolve_root_mapping(root, known_paths=known)
    reg = build_source_registry(mapping, preferred)

    if hasattr(plan, "source_registry"):
        plan.source_registry = reg.to_dict()
        plan.source_candidates = reg.source_ids()
    elif isinstance(plan, dict):
        plan["source_registry"] = reg.to_dict()
        plan["source_candidates"] = reg.source_ids()

    return reg if reg.available() else None


def inject_source_tools(out: dict[str, Any], plan: Any) -> bool:
    """Replace path-based tools with ReadSource/GrepSource/GlobSource when registry exists."""
    reg = registry_from_plan(plan)
    if not reg:
        reg = _refresh_registry_on_plan(plan)
    if not reg:
        return False
    ids = reg.source_ids()
    if not ids:
        return False
    out["tools"] = llm_source_tool_definitions(ids)
    return True


def _expand_one_tool(
    tool_name: str,
    args: dict[str, Any],
    reg: SourceRegistry,
) -> tuple[str, dict[str, Any]] | None:
    if tool_name in SOURCE_TOOL_NAMES:
        return expand_source_tool_call(tool_name, args, reg)

    if tool_name not in LEGACY_PATH_TOOLS:
        return None

    path_hint = str(args.get("path") or args.get("target_directory") or "")
    if not path_hint and tool_name == "Glob":
        path_hint = str(args.get("glob_pattern") or "")

    if not path_hint:
        return None

    try:
        resolved, sid = resolve_path_via_registry(reg, path_hint, for_cursor=True)
    except (KeyError, FileNotFoundError, ValueError) as exc:
        LOG.warning("path_resolve_failed tool=%s hint=%r err=%s", tool_name, path_hint[:80], exc)
        return None

    out_args: dict[str, Any] = dict(args)
    if tool_name == "Read":
        out_args = {"path": resolved, "_source_id": sid}
    elif tool_name == "Grep":
        out_args = {
            "pattern": str(args.get("pattern") or args.get("query") or ""),
            "path": resolved,
            "_source_id": sid,
        }
    elif tool_name == "Glob":
        out_args = {
            "glob_pattern": str(args.get("glob_pattern") or "**/*"),
            "target_directory": resolved,
            "_source_id": sid,
        }
    return tool_name, out_args


def expand_source_tool_calls_in_response(
    response: dict[str, Any],
    plan: Any,
) -> dict[str, Any]:
    """Translate source_id or registry-relative paths to Cursor-executable paths."""
    reg = registry_from_plan(plan)
    if not reg:
        reg = _refresh_registry_on_plan(plan)
    if not reg:
        return response
    try:
        msg = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return response

    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        return response

    out_tcs: list[dict[str, Any]] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            out_tcs.append(tc)
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}

        if args.get("_source_id") and name in LEGACY_PATH_TOOLS:
            out_tcs.append(tc)
            continue

        expanded = _expand_one_tool(name, args, reg)
        if not expanded:
            if name in LEGACY_PATH_TOOLS and reg.source_candidates:
                LOG.warning("dropped_unresolved_path_tool tool=%s args=%r", name, args)
                continue
            out_tcs.append(tc)
            continue

        cursor_name, cursor_args = expanded
        out_tcs.append(
            {
                "id": str(tc.get("id") or f"call_src_{secrets.token_hex(8)}"),
                "type": "function",
                "function": {
                    "name": cursor_name,
                    "arguments": json.dumps(cursor_args, ensure_ascii=False),
                },
            }
        )
        LOG.info(
            "source_expand %s -> %s(%s) sid=%s",
            name,
            cursor_name,
            str(cursor_args.get("path", cursor_args.get("target_directory", "")))[:80],
            cursor_args.get("_source_id", ""),
        )

    msg["tool_calls"] = out_tcs
    return response


def resolve_xml_tool_args(tool_name: str, args: dict[str, Any], plan: Any) -> dict[str, Any]:
    """Resolve XML-recovered tool args through registry when possible."""
    reg = registry_from_plan(plan)
    if not reg:
        reg = _refresh_registry_on_plan(plan)
    if not reg:
        return args
    expanded = _expand_one_tool(tool_name, args, reg)
    if expanded:
        _name, out_args = expanded
        return out_args
    if tool_name == "Read":
        path = str(args.get("path") or "")
        sid = lookup_source_id_by_relpath(reg, path)
        if sid:
            try:
                resolved, sid2 = resolve_path_via_registry(reg, path, for_cursor=True)
                return {"path": resolved, "_source_id": sid2 or sid}
            except (KeyError, FileNotFoundError, ValueError):
                pass
    return args


def extract_source_id_from_args(args: dict[str, Any]) -> str:
    return str(args.get("_source_id") or args.get("source_id") or "").strip()


def _source_id_from_tool_call(tool_name: str, args: dict[str, Any]) -> str:
    sid = extract_source_id_from_args(args)
    if sid:
        return sid
    if tool_name in SOURCE_TOOL_NAMES:
        return str(args.get("source_id") or "").strip()
    return ""


def filter_redundant_source_tool_calls(
    response: dict[str, Any],
    plan: Any,
) -> tuple[dict[str, Any], int]:
    """Drop repeated source tool calls — read_only uses action signatures, not bare source_hits."""
    plan_dict: dict[str, Any] = {}
    if hasattr(plan, "to_dict"):
        plan_dict = plan.to_dict()
    elif isinstance(plan, dict):
        plan_dict = plan
    elif hasattr(plan, "source_hits"):
        plan_dict = {
            "source_hits": list(plan.source_hits or []),
            "router_intent": str(getattr(plan, "router_intent", "") or ""),
            "exploration_actions_tried": list(getattr(plan, "exploration_actions_tried", None) or []),
            "source_exploration_stage": dict(getattr(plan, "source_exploration_stage", None) or {}),
            "source_registry": dict(getattr(plan, "source_registry", None) or {}),
        }

    hits: set[str] = set(plan_dict.get("source_hits") or [])
    router_intent = str(plan_dict.get("router_intent") or "")
    read_only = router_intent == "read_only_analysis"

    if not hits and not read_only:
        return response, 0

    try:
        msg = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return response, 0

    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        return response, 0

    tried: set[str] = set()
    reg = None
    if read_only:
        try:
            from .read_only_explorer import actions_tried_set, exploration_action_sig

            tried = actions_tried_set(plan_dict)
            from .source_registry import SourceRegistry

            reg = SourceRegistry.from_dict(plan_dict.get("source_registry") or {})
        except ImportError:
            read_only = False

    kept: list[dict[str, Any]] = []
    seen_sids: set[str] = set()
    removed = 0
    for tc in tool_calls:
        if not isinstance(tc, dict):
            kept.append(tc)
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        sid = _source_id_from_tool_call(name, args)
        if read_only and sid and reg is not None:
            pat = str(args.get("pattern") or "")
            glob_pat = str(args.get("glob_pattern") or "")
            sig = exploration_action_sig(name, sid, pattern=pat, glob_pattern=glob_pat)
            if sig in tried:
                removed += 1
                continue
            entry = reg.get(sid)
            if (
                name in ("ReadSource", "Read")
                and sid in hits
                and entry
                and entry.kind == "file"
            ):
                removed += 1
                continue
        elif sid and sid in hits:
            removed += 1
            continue
        if sid and sid in seen_sids:
            removed += 1
            continue
        if sid:
            seen_sids.add(sid)
        kept.append(tc)

    if removed:
        LOG.info("source_filter removed=%d kept=%d hits=%s read_only=%s", removed, len(kept), list(hits)[:6], read_only)
    msg["tool_calls"] = kept
    return response, removed


def pick_next_read_only_tool(plan: Any) -> tuple[str, dict[str, Any]] | None:
    """One deterministic tool per turn — Runtime picks inventory (Glob) for dirs."""
    reg = registry_from_plan(plan)
    if not reg:
        reg = _refresh_registry_on_plan(plan)
    if not reg:
        return None
    from .source_registry import pending_source_ids_for_plan

    plan_dict = plan.to_dict() if hasattr(plan, "to_dict") else dict(plan or {})
    pending = pending_source_ids_for_plan(plan_dict, reg)
    if not pending:
        return None
    for sid in pending:
        entry = reg.get(sid)
        if not entry:
            continue
        if entry.kind == "file" or sid.startswith("doc."):
            return "ReadSource", {"source_id": sid}
        from .source_registry import dir_inventory_tool_for_source

        tool = dir_inventory_tool_for_source(plan_dict, sid)
        if tool == "GlobSource":
            return "GlobSource", {"source_id": sid, "glob_pattern": "*.py"}
        return "GrepSource", {"source_id": sid, "pattern": WIDE_DIR_GREP_PATTERN}
    return None


def dedupe_identical_tool_calls(
    response: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Collapse duplicate tool_calls (same tool + args) in one assistant message."""
    try:
        msg = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return response, 0

    tool_calls = msg.get("tool_calls") or []
    if len(tool_calls) < 2:
        return response, 0

    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    removed = 0
    for tc in tool_calls:
        if not isinstance(tc, dict):
            kept.append(tc)
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        key = f"{name}:{json.dumps(args, sort_keys=True)}"
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        kept.append(tc)

    if removed:
        LOG.info("tool_dedupe removed=%d kept=%d", removed, len(kept))
        msg["tool_calls"] = kept
    return response, removed
