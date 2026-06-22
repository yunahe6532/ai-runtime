"""Query-target coverage for read-only analysis (not Shell string matching)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

READ_ONLY_KW = (
    "코드 수정 말고",
    "수정하지 말",
    "읽어서",
    "read only",
    "read-only",
    "don't edit",
    "do not edit",
    "no edit",
)

SHELL_EXPLICIT_KW = (
    "명령 실행",
    "shell로",
    "shell ",
    "터미널에서",
    "run command",
    "execute command",
)


def looks_like_read_only_query(query: str) -> bool:
    q = (query or "").lower()
    if any(k in query for k in READ_ONLY_KW):
        return True
    if "분석" in query and any(k in q for k in ("구조", "역할", "요약", "structure", "architecture")):
        return True
    return False


def shell_explicitly_requested(query: str) -> bool:
    q = (query or "").lower()
    return any(k in q for k in SHELL_EXPLICIT_KW)


def extract_structure_coverage_targets(query: str, project_root: str) -> list[str]:
    """Discover coverage targets from query + repo layout (no preset module/doc list)."""
    from .project_root import effective_workspace
    from .source_registry import discover_read_only_relpaths, resolve_root_mapping

    root = effective_workspace(project_root, [])
    mapping = resolve_root_mapping(root, known_paths=[])
    relpaths, _summary = discover_read_only_relpaths(query, mapping)
    return relpaths


def _norm_target(t: str) -> str:
    return str(t).replace("\\", "/").strip().lower().rstrip("/")


def coverage_target_tokens(target: str) -> set[str]:
    """Expand source_id / relpath targets for path and retrieval matching."""
    raw = (target or "").strip()
    if not raw:
        return set()
    norm = _norm_target(raw)
    tokens = {norm, raw.lower()}
    if norm.startswith("dir."):
        rel = norm[4:]
        if rel:
            tokens.add(rel)
            tokens.add(f"{rel}/")
    else:
        tokens.add(f"dir.{norm}")
    if "/" in norm:
        tokens.add(Path(norm).name.lower())
    return {t for t in tokens if t}


def coverage_target_in_text(target: str, text: str) -> bool:
    blob = (text or "").lower()
    if not blob:
        return False
    return any(tok in blob for tok in coverage_target_tokens(target))


def target_matches_path(target: str, path: str, project_root: str) -> bool:
    if not path:
        return False
    t = _norm_target(target)
    if t.startswith("dir."):
        t = t[4:]
    p = path.replace("\\", "/").lower()
    root = project_root.replace("\\", "/").lower().rstrip("/")
    if p.startswith(root + "/"):
        p = p[len(root) + 1 :]
    elif p.startswith("/"):
        p = p.lstrip("/")
    if t.endswith(".md"):
        return Path(p).name.lower() == Path(t).name.lower() or t in p
    return p == t or p.startswith(t + "/") or t in p


def register_hits_from_tool(
    plan_dict: dict[str, Any],
    *,
    path: str,
    content: str,
    tool_name: str,
    project_root: str,
    targets: list[str],
    success: bool = True,
    source_id: str = "",
    pattern: str = "",
    registry: dict[str, Any] | None = None,
) -> list[str]:
    """Record coverage — successful tool results only."""
    from .source_registry import is_tool_result_success, register_source_hit

    if tool_name == "Shell":
        return []
    if not success or not is_tool_result_success(content, tool_name=tool_name):
        return []

    if source_id and registry:
        return register_source_hit(
            plan_dict,
            source_id,
            success=True,
            content=content,
            registry=registry,
            tool_name=tool_name,
            pattern=pattern,
        )

    added: list[str] = []
    for target in targets:
        if target_matches_path(target, path, project_root):
            before = {_norm_target(h) for h in (plan_dict.get("coverage_hits") or [])}
            if _norm_target(target) not in before:
                register_target_hit(plan_dict, target)
                added.append(target)
    return added


def score_target_coverage(plan_dict: dict[str, Any], targets: list[str]) -> tuple[float, list[str], list[str]]:
    if not targets:
        return 1.0, [], []
    hits = {_norm_target(h) for h in (plan_dict.get("coverage_hits") or [])}
    module_targets = [t for t in targets if not t.endswith(".md")]
    doc_targets = [t for t in targets if t.endswith(".md")]
    hit_list = [t for t in targets if _norm_target(t) in hits]
    missing = [t for t in targets if _norm_target(t) not in hits]

    if module_targets:
        mod_hits = sum(1 for t in module_targets if _norm_target(t) in hits)
        mod_score = mod_hits / float(len(module_targets))
        doc_hits = sum(1 for t in doc_targets if _norm_target(t) in hits)
        doc_score = (doc_hits / float(len(doc_targets))) if doc_targets else 1.0
        score = min(mod_score, doc_score)
        return score, hit_list, missing

    if doc_targets:
        doc_hits = sum(1 for t in doc_targets if _norm_target(t) in hits)
        score = doc_hits / float(len(doc_targets))
        return score, hit_list, missing

    return (1.0 if hit_list else 0.0), hit_list, missing


def target_coverage_passes(plan_dict: dict[str, Any], targets: list[str]) -> bool:
    if not targets:
        return True
    hits = {_norm_target(h) for h in (plan_dict.get("coverage_hits") or [])}
    module_targets = [t for t in targets if not t.endswith(".md")]
    doc_targets = [t for t in targets if t.endswith(".md")]
    if module_targets:
        if any(_norm_target(t) not in hits for t in module_targets):
            return False
        if doc_targets and any(_norm_target(t) not in hits for t in doc_targets):
            return False
        return True
    if doc_targets:
        return all(_norm_target(t) in hits for t in doc_targets)
    score, _, _ = score_target_coverage(plan_dict, targets)
    return score >= 0.75


def register_target_hit(plan_dict: dict[str, Any], target: str) -> None:
    hits = list(plan_dict.get("coverage_hits") or [])
    nt = _norm_target(target)
    if nt not in {_norm_target(h) for h in hits}:
        hits.append(target)
    plan_dict["coverage_hits"] = hits


def read_only_tool_policy(query: str, *, use_source_tools: bool = True) -> tuple[list[str], list[str], int]:
    """allowed, disallowed, max_tool_rounds."""
    if use_source_tools:
        allowed = ["ReadSource", "GrepSource", "GlobSource"]
        disallowed = [
            "Read",
            "Grep",
            "Glob",
            "Shell",
            "Write",
            "StrReplace",
            "Delete",
            "Edit",
            "ApplyPatch",
            "NotebookEdit",
        ]
    else:
        allowed = ["Read", "Grep", "Glob"]
        disallowed = [
            "Shell",
            "Write",
            "StrReplace",
            "Delete",
            "Edit",
            "ApplyPatch",
            "NotebookEdit",
        ]
    if shell_explicitly_requested(query):
        allowed.append("Shell")
        disallowed = [t for t in disallowed if t != "Shell"]
    return allowed, disallowed, 24


def is_home_shell_command(command: str) -> bool:
    cmd = (command or "").strip()
    if not cmd:
        return False
    return bool(re.search(r"(?:^|\s)(?:ls|find|tree)\s+.*(/home/\w+|~)(?:\s|$)", cmd))
