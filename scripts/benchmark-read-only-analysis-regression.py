#!/usr/bin/env python3
"""Regression: read_only_analysis must not force Shell or finalize without coverage."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from adapters.memory import SessionState  # noqa: E402
from reference.planner import (  # noqa: E402
    AgentPlan,
    apply_policy_constraints,
    build_rule_plan,
    validate_tool_call,
)
from reference.plan_state import resolve_agent_phase  # noqa: E402
from reference.project_root import is_home_directory, resolve_project_root  # noqa: E402
from reference.source_registry import build_source_registry, resolve_root_mapping  # noqa: E402
from reference.source_registry import (  # noqa: E402
    register_source_hit,
    summary_source_ids_for_registry,
)
from reference.target_coverage import (  # noqa: E402
    extract_structure_coverage_targets,
    read_only_tool_policy,
    register_hits_from_tool,
    score_target_coverage,
    target_coverage_passes,
)


STRUCTURE_QUERY = (
    "이 프로젝트 구조를 분석해서 runtime_core, adapters, legacy, integrations 역할을 요약해줘. "
    "코드는 수정하지 말고 필요한 파일만 읽어서 근거와 함께 답해."
)


def _wide_grep_body(root: Path, relpath: str) -> str:
    """Synthetic wide grep workspace_result with enough files for shallow-hit gate."""
    target = root / relpath
    lines = [f'<workspace_result workspace_path="{root}">']
    if target.is_dir():
        files = sorted(target.glob("*.py")) or sorted(target.glob("*"))
        for fp in files[:12]:
            rel = fp.relative_to(root).as_posix()
            lines.append(rel)
            lines.append(f'  1:"""Module {fp.stem}"""')
    else:
        lines.append(relpath)
        lines.append('  1:"""doc"""')
    lines.append("</workspace_result>")
    return "\n".join(lines)


def _register_dir_exploration_hits(
    plan_dict: dict,
    reg,
    source_id: str,
    relpath: str,
    *,
    root: Path = ROOT,
) -> None:
    from reference.read_only_explorer import BOUNDARY_GREP, WIDE_CONTENT_GREP

    target = root / relpath
    paths = sorted(target.glob("*.py")) if target.is_dir() else []
    glob_body = "Result of search in {} (total {} files):\n".format(relpath, len(paths))
    glob_body += "\n".join(str(p) for p in paths)
    wide = _wide_grep_body(root, relpath)

    register_source_hit(
        plan_dict,
        source_id,
        success=True,
        content=glob_body,
        registry=reg,
        tool_name="Glob",
    )
    register_source_hit(
        plan_dict,
        source_id,
        success=True,
        content=wide,
        registry=reg,
        tool_name="Grep",
        pattern=WIDE_CONTENT_GREP,
    )
    boundary = _wide_grep_body(root, relpath).replace("runtime_core", "runtime_core\nrouter/legacy/memory_store.py")
    register_source_hit(
        plan_dict,
        source_id,
        success=True,
        content=boundary,
        registry=reg,
        tool_name="Grep",
        pattern=BOUNDARY_GREP,
    )


def _simulate_sufficient_exploration(plan_dict: dict, reg, root: Path = ROOT) -> None:
    """Simulate docs + per-dir depth for can_final_answer (LLM-chosen path agnostic)."""
    summary_ids = list(plan_dict.get("summary_source_ids") or [])
    for sid in summary_ids:
        entry = reg.get(sid)
        if not entry:
            continue
        content = "x" * 40
        doc_path = root / entry.relpath
        if doc_path.is_file():
            try:
                content = doc_path.read_text(encoding="utf-8")[:2000]
            except OSError:
                pass
        register_source_hit(
            plan_dict,
            sid,
            success=True,
            content=content,
            registry=reg,
            tool_name="Read",
        )

    seen: set[str] = set()
    for sid in list(plan_dict.get("required_source_ids") or []) + list(reg.source_ids()):
        if sid in seen:
            continue
        entry = reg.get(sid)
        if not entry or entry.kind != "dir":
            continue
        seen.add(sid)
        _register_dir_exploration_hits(plan_dict, reg, sid, entry.relpath, root=root)

    from reference.read_only_explorer import record_exploration_milestone

    record_exploration_milestone(plan_dict, "cross_tier:imports")


def test_read_only_analysis_no_shell_by_default() -> None:
    allowed, disallowed, _max_rounds = read_only_tool_policy(STRUCTURE_QUERY, use_source_tools=True)
    assert "Shell" in disallowed
    assert "ReadSource" in allowed
    assert "Read" in disallowed

    state = SessionState(workspace_path=str(ROOT))
    plan = build_rule_plan(STRUCTURE_QUERY, state, router_intent="read_only_analysis")
    assert plan.next_action.get("tool") != "Shell", plan.next_action
    assert plan.source_registry
    assert plan.source_candidates

    ok, reason = validate_tool_call(
        "Read",
        {"path": "/home/yunahe/ai-runt/docs/MODULE_MAP.md"},
        plan,
    )
    assert not ok, reason
    print("test_read_only_analysis_no_shell_by_default: OK")


def test_project_structure_discovered_from_query() -> None:
    from reference.source_registry import discover_read_only_relpaths, resolve_root_mapping

    root = resolve_project_root(str(ROOT))
    mapping = resolve_root_mapping(str(root), known_paths=[str(ROOT / "docs" / "MODULE_MAP.md")])
    relpaths, summary = discover_read_only_relpaths(STRUCTURE_QUERY, mapping)
    assert relpaths, relpaths
    assert any("MODULE_MAP" in t or "ARCHITECTURE" in t for t in relpaths), relpaths
    assert any("runtime_core" in t for t in relpaths), relpaths
    assert summary, summary

    plan = apply_policy_constraints(
        AgentPlan(task_intent="project_inspection", goal=STRUCTURE_QUERY),
        STRUCTURE_QUERY,
        router_intent="read_only_analysis",
        workspace=str(ROOT),
    )
    assert plan.source_registry
    assert plan.summary_source_ids
    assert plan.required_source_ids
    print("test_project_structure_discovered_from_query: OK")


def test_home_directory_not_project_root() -> None:
    home = "/home/yunahe"
    resolved = resolve_project_root(home)
    assert not is_home_directory(Path(resolved)), resolved
    assert "router" in resolved or "cursor-local-llm" in resolved, resolved
    print("test_home_directory_not_project_root: OK")


def test_answer_action_requires_target_coverage() -> None:
    state = SessionState()
    state.agent_plan = {
        "task_intent": "project_inspection",
        "router_intent": "read_only_analysis",
        "next_action": {"tool": "answer", "target": "", "reason": "done"},
        "evidence_needed": ["target_coverage"],
        "evidence_collected": [],
        "coverage_hits": [],
        "preferred_sources": [
            "docs/MODULE_MAP.md",
            "docs/ARCHITECTURE.md",
            "router/runtime_core",
            "router/adapters",
        ],
        "final_ready": True,
    }
    state.last_ingest_metrics = {"diff_mode": "append_only", "messages_new": 1}

    body = {
        "messages": [
            {"role": "user", "content": STRUCTURE_QUERY},
            {"role": "tool", "name": "Read", "content": "ok"},
        ]
    }
    phase_no_cov = resolve_agent_phase(body, state, STRUCTURE_QUERY, "read_only_analysis", True)
    assert phase_no_cov != "final_answer", phase_no_cov

    plan_dict = dict(state.agent_plan)
    mapping = resolve_root_mapping(str(ROOT))
    reg = build_source_registry(mapping, plan_dict["preferred_sources"] + ["router"])
    plan_dict["source_registry"] = reg.to_dict()
    plan_dict["source_candidates"] = reg.source_ids()
    plan_dict["summary_source_ids"] = summary_source_ids_for_registry(reg, ["docs/MODULE_MAP.md", "docs/ARCHITECTURE.md"])
    plan_dict["required_source_ids"] = list(
        dict.fromkeys(plan_dict["summary_source_ids"] + ["dir.runtime_core", "dir.adapters"])
    )

    grep_body = _wide_grep_body(ROOT, "router/adapters")
    register_source_hit(
        plan_dict,
        "doc.module_map",
        success=True,
        content=(ROOT / "docs" / "MODULE_MAP.md").read_text(encoding="utf-8")[:2000],
        registry=reg,
    )
    register_source_hit(
        plan_dict,
        "doc.architecture",
        success=True,
        content=(ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")[:2000],
        registry=reg,
    )
    _simulate_sufficient_exploration(plan_dict, reg)
    for rel in ("router/runtime_core", "router/adapters"):
        register_hits_from_tool(
            plan_dict,
            path=str(ROOT / rel),
            content=grep_body,
            tool_name="Grep",
            project_root=str(ROOT),
            targets=plan_dict["preferred_sources"],
            success=True,
        )
    state.agent_plan = plan_dict
    state.agent_plan["final_ready"] = True
    state.agent_plan["next_action"] = {"tool": "answer", "target": "", "reason": "done"}
    assert target_coverage_passes(plan_dict, plan_dict["preferred_sources"])

    phase_cov = resolve_agent_phase(body, state, STRUCTURE_QUERY, "read_only_analysis", True)
    assert phase_cov == "final_answer", phase_cov
    print("test_answer_action_requires_target_coverage: OK")


def test_target_coverage_evidence_not_string_only() -> None:
    from reference.evidence_extractors import evidence_types_satisfied

    assert not evidence_types_satisfied(
        ["target_coverage"],
        [],
        task_intent="project_inspection",
    )
    assert evidence_types_satisfied(
        ["target_coverage"],
        ["source_hit:doc.module_map"],
        task_intent="project_inspection",
    )
    print("test_target_coverage_evidence_not_string_only: OK")


def test_read_only_phase_uses_coverage_not_string_evidence() -> None:
    from reference.plan_state import resolve_agent_phase
    from reference.source_registry import register_source_hit

    state = SessionState()
    body = {
        "messages": [
            {"role": "user", "content": STRUCTURE_QUERY},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "Read"}}]},
            {"role": "tool", "content": "ok module map content " * 20},
        ]
    }
    mapping = resolve_root_mapping(str(ROOT), known_paths=[str(ROOT / "docs/MODULE_MAP.md")])
    reg = build_source_registry(
        mapping,
        [
            "docs/MODULE_MAP.md",
            "docs/ARCHITECTURE.md",
            "router",
            "router/runtime_core",
            "router/adapters",
            "router/legacy",
            "router/integrations",
        ],
    )
    grep_body = _wide_grep_body(ROOT, "router/runtime_core")
    plan_dict = {
        "task_intent": "general",
        "router_intent": "read_only_analysis",
        "evidence_needed": ["target_coverage"],
        "evidence_collected": ["source_hit:doc.module_map"],
        "source_hits": [],
        "source_candidates": reg.source_ids(),
        "preferred_sources": extract_structure_coverage_targets(STRUCTURE_QUERY, str(ROOT)),
        "source_registry": reg.to_dict(),
        "summary_source_ids": summary_source_ids_for_registry(reg, ["docs/MODULE_MAP.md", "docs/ARCHITECTURE.md"]),
        "required_source_ids": reg.source_ids(),
        "final_ready": True,
        "next_action": {"tool": "answer", "target": "", "reason": "done"},
    }
    _simulate_sufficient_exploration(plan_dict, reg)
    for rel in ("router/runtime_core", "router/adapters", "router/legacy", "router/integrations"):
        register_hits_from_tool(
            plan_dict,
            path=str(ROOT / rel),
            content=grep_body,
            tool_name="Grep",
            project_root=str(ROOT),
            targets=plan_dict["preferred_sources"],
            success=True,
        )
    state.agent_plan = plan_dict
    phase = resolve_agent_phase(body, state, STRUCTURE_QUERY, "read_only_analysis", True)
    assert phase == "final_answer", phase
    print("test_read_only_phase_uses_coverage_not_string_evidence: OK")


def test_read_only_docs_sufficient_passes() -> None:
    from reference.source_registry import (
        build_source_registry,
        discover_read_only_relpaths,
        read_only_docs_sufficient,
        register_source_hit,
        resolve_root_mapping,
        summary_source_ids_for_registry,
    )

    mapping = resolve_root_mapping(str(ROOT), known_paths=[str(ROOT / "docs" / "MODULE_MAP.md")])
    relpaths, summary = discover_read_only_relpaths(STRUCTURE_QUERY, mapping)
    reg = build_source_registry(mapping, relpaths)
    summary_ids = summary_source_ids_for_registry(reg, summary)
    plan_dict = {
        "router_intent": "read_only_analysis",
        "source_hits": [],
        "summary_source_ids": summary_ids,
    }
    assert not read_only_docs_sufficient(plan_dict, reg)
    for sid in summary_ids:
        register_source_hit(
            plan_dict,
            sid,
            success=True,
            content="x" * 40,
            registry=reg,
        )
    assert read_only_docs_sufficient(plan_dict, reg)
    print("test_read_only_docs_sufficient_passes: OK")


def test_no_empty_response() -> None:
    from reference.response_guard import is_empty_outgoing

    resp = {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": []}}]}
    assert is_empty_outgoing(resp)
    resp2 = {"choices": [{"message": {"role": "assistant", "content": "요약입니다.", "tool_calls": []}}]}
    assert not is_empty_outgoing(resp2)
    print("test_no_empty_response: OK")


def test_final_ready_after_tool_results() -> None:
    state = SessionState()
    state.agent_plan = {
        "task_intent": "project_inspection",
        "router_intent": "read_only_analysis",
        "next_action": {"tool": "answer", "target": "", "reason": "done"},
        "evidence_needed": ["target_coverage"],
        "evidence_collected": ["architecture_doc_seen", "core_files_seen"],
        "coverage_hits": ["docs/MODULE_MAP.md", "docs/ARCHITECTURE.md"],
        "preferred_sources": [
            "docs/MODULE_MAP.md",
            "docs/ARCHITECTURE.md",
            "router/runtime_core",
            "router/adapters",
        ],
        "final_ready": True,
    }
    mapping = resolve_root_mapping(str(ROOT))
    reg = build_source_registry(mapping, state.agent_plan["preferred_sources"])
    state.agent_plan["source_registry"] = reg.to_dict()
    from reference.source_registry import register_source_hit

    content = (ROOT / "docs" / "MODULE_MAP.md").read_text(encoding="utf-8")[:2000]
    register_source_hit(
        state.agent_plan,
        "doc.module_map",
        success=True,
        content=content,
        registry=reg,
    )
    register_source_hit(
        state.agent_plan,
        "doc.architecture",
        success=True,
        content=(ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")[:2000],
        registry=reg,
    )
    state.phase_state = {
        "final_ready": True,
        "last_role": "tool",
        "tool_call_turns_since_user": 1,
        "last_user_query_key": "q1",
        "final_ready_query_key": "q1",
    }
    state.last_ingest_metrics = {"diff_mode": "append_only", "messages_new": 5}

    body = {
        "messages": [
            {"role": "user", "content": STRUCTURE_QUERY},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
            {"role": "tool", "name": "Read", "content": "ok"},
        ]
    }
    phase = resolve_agent_phase(body, state, STRUCTURE_QUERY, "read_only_analysis", True)
    assert phase == "final_answer", phase
    print("test_final_ready_after_tool_results: OK")


def test_partial_module_coverage_does_not_pass() -> None:
    plan_dict = {
        "coverage_hits": ["docs/MODULE_MAP.md", "docs/ARCHITECTURE.md", "router/runtime_core"],
        "preferred_sources": [
            "docs/MODULE_MAP.md",
            "docs/ARCHITECTURE.md",
            "router/runtime_core",
            "router/adapters",
            "router/legacy",
            "router/integrations",
        ],
    }
    targets = plan_dict["preferred_sources"]
    assert not target_coverage_passes(plan_dict, targets)
    score, _, missing = score_target_coverage(plan_dict, targets)
    assert score < 1.0
    assert "router/legacy" in missing or "router/integrations" in missing
    print("test_partial_module_coverage_does_not_pass: OK")


def test_full_structure_coverage_passes() -> None:
    plan_dict = {
        "coverage_hits": [
            "docs/MODULE_MAP.md",
            "docs/ARCHITECTURE.md",
            "router/runtime_core",
            "router/adapters",
            "router/legacy",
            "router/integrations",
        ],
        "preferred_sources": [
            "docs/MODULE_MAP.md",
            "docs/ARCHITECTURE.md",
            "router/runtime_core",
            "router/adapters",
            "router/legacy",
            "router/integrations",
        ],
    }
    assert target_coverage_passes(plan_dict, plan_dict["preferred_sources"])
    print("test_full_structure_coverage_passes: OK")


def test_read_only_injects_tools_without_cursor_tools() -> None:
    """Regression: compressed Cursor body often has tools=[] — runtime must inject ReadSource."""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "router"
    sys.path.insert(0, str(root))

    from intent_router import IntentResult, _apply_tools_policy
    from reference.planner import AgentPlan, apply_policy_constraints
    from adapters.memory import SessionState

    query = STRUCTURE_QUERY
    state = SessionState()
    plan = apply_policy_constraints(
        AgentPlan(goal=query),
        query,
        router_intent="read_only_analysis",
        workspace=str(ROOT),
    )
    state.agent_plan = plan.to_dict()
    intent = IntentResult(
        intent="read_only_analysis",
        route="main",
        needs_tools=True,
        needs_files=True,
        needs_shell=False,
        needs_prior_summary=False,
        needs_raw_tool_results=False,
        needs_full_raw_context=False,
        context_budget_tokens=8000,
        context_pack=[],
    )
    out: dict = {"messages": [{"role": "user", "content": query}]}
    body: dict = {"messages": out["messages"], "tools": []}
    stripped = _apply_tools_policy(out, body, intent, query, "tool_planning", state)
    assert not stripped, (stripped, out.get("tools"))
    names = [
        str((t.get("function") or {}).get("name") or "")
        for t in (out.get("tools") or [])
        if isinstance(t, dict)
    ]
    assert any(n in ("ReadSource", "GlobSource", "GrepSource") for n in names), names
    print("test_read_only_injects_tools_without_cursor_tools: OK")


def test_json_tool_calls_parsed_from_content() -> None:
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "router"
    sys.path.insert(0, str(root))

    from reference.response_guard import parse_json_tool_calls_from_content

    content = (
        '{\n    "tool_calls": [\n'
        '        {"name": "GrepSource", "arguments": {"source_id": "dir.runtime_core", "pattern": "."}}\n'
        "    ]\n}"
    )
    calls = parse_json_tool_calls_from_content(content)
    assert calls and calls[0][0] == "GrepSource", calls
    assert calls[0][1].get("source_id") == "dir.runtime_core", calls
    print("test_json_tool_calls_parsed_from_content: OK")


def test_json_tool_name_field_parsed() -> None:
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "router"
    sys.path.insert(0, str(root))

    from reference.response_guard import parse_json_tool_calls_from_content

    content = (
        '{\n  "tool_calls": [\n'
        '    {"tool_name": "GlobSource", "arguments": {"source_id": "runtime_core", "glob_pattern": "*"}}\n'
        "  ]\n}"
    )
    calls = parse_json_tool_calls_from_content(content)
    assert calls and calls[0][0] == "GlobSource", calls
    assert calls[0][1].get("source_id") == "runtime_core", calls
    print("test_json_tool_name_field_parsed: OK")


def test_empty_glob_does_not_register_source_hit() -> None:
    from reference.source_registry import (
        RootMapping,
        build_source_registry,
        is_tool_result_success,
        register_source_hit,
    )

    mapping = RootMapping(host=str(ROOT), container=str(ROOT / "router"), confidence=0.9, method="test")
    reg = build_source_registry(mapping, ["router/runtime_core"])
    plan_dict: dict = {"source_hits": [], "coverage_hits": []}
    empty = "Result of search in '/path': 0 files found"
    assert not is_tool_result_success(empty, tool_name="Glob")
    added = register_source_hit(
        plan_dict,
        "dir.runtime_core",
        success=True,
        content=empty,
        registry=reg,
        tool_name="Glob",
    )
    assert added == [], added
    assert plan_dict.get("source_hits") == []
    print("test_empty_glob_does_not_register_source_hit: OK")


def test_read_only_no_early_final_after_empty_globs() -> None:
    from adapters.memory import SessionState
    from reference.plan_state import resolve_agent_phase
    from reference.planner import AgentPlan, apply_policy_constraints, update_plan_after_tool

    q = STRUCTURE_QUERY
    plan = apply_policy_constraints(AgentPlan(goal=q), q, router_intent="read_only_analysis", workspace=str(ROOT))
    state = SessionState()
    state.agent_plan = plan.to_dict()
    empty = "Result of search in '/path': 0 files found"
    for sid, path in (
        ("dir.runtime_core", str(ROOT / "router" / "runtime_core")),
        ("dir.adapters", str(ROOT / "router" / "adapters")),
    ):
        plan = update_plan_after_tool(
            plan,
            state,
            tool_name="Glob",
            args={"target_directory": path, "glob_pattern": "*.md", "_source_id": sid},
            result_text=empty,
            success=True,
        )
    state.agent_plan = plan.to_dict()
    body = {
        "messages": [
            {"role": "user", "content": q},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "Glob", "arguments": "{}"}}]},
            {"role": "tool", "name": "Glob", "content": empty},
        ]
    }
    phase = resolve_agent_phase(body, state, q, "read_only_analysis", True)
    assert phase == "tool_planning", phase
    print("test_read_only_no_early_final_after_empty_globs: OK")


def test_read_only_explorer_static_ladder() -> None:
    from reference.read_only_explorer import plan_read_only_exploration
    from reference.planner import AgentPlan, apply_policy_constraints

    q = STRUCTURE_QUERY
    plan = apply_policy_constraints(
        AgentPlan(goal=q),
        q,
        router_intent="read_only_analysis",
        workspace=str(ROOT),
    )
    os.environ["READ_ONLY_EXPLORER_ENABLED"] = "0"
    try:
        decision = plan_read_only_exploration(None, plan, q)
    finally:
        os.environ.pop("READ_ONLY_EXPLORER_ENABLED", None)
    assert decision.tool in ("ReadSource", "GlobSource", "GrepSource"), (
        decision.tool,
        decision.source_id,
        decision.pattern,
    )
    assert decision.source_id
    assert not decision.allow_final
    print("test_read_only_explorer_static_ladder: OK")


def test_wide_grep_pattern_forces_on_dir() -> None:
    from reference.source_registry import (
        WIDE_DIR_GREP_PATTERN,
        build_source_registry,
        expand_source_tool_call,
        resolve_root_mapping,
    )

    mapping = resolve_root_mapping(str(ROOT))
    reg = build_source_registry(mapping, ["router/adapters"])
    out = expand_source_tool_call(
        "GrepSource",
        {"source_id": "dir.adapters", "pattern": '"""docstring only"""'},
        reg,
    )
    assert out is not None
    name, args = out
    assert name == "Grep"
    assert args.get("pattern") == '"""docstring only"""'
    print("test_wide_grep_pattern_forces_on_dir: OK")


def test_shallow_dir_grep_does_not_register_hit() -> None:
    from reference.source_registry import (
        build_source_registry,
        register_source_hit,
        resolve_root_mapping,
        source_coverage_passes,
    )

    mapping = resolve_root_mapping(str(ROOT))
    reg = build_source_registry(mapping, ["router/adapters"])
    plan_dict = {
        "router_intent": "read_only_analysis",
        "required_source_ids": ["dir.adapters"],
        "source_hits": [],
        "source_grep_depth": {},
        "source_registry": reg.to_dict(),
    }
    shallow = '<workspace_result>\nrouter/adapters/gateway.py\n  1:"""only one"""\n</workspace_result>'
    added = register_source_hit(
        plan_dict,
        "dir.adapters",
        success=True,
        content=shallow,
        registry=reg,
        tool_name="Grep",
    )
    assert added == []
    assert "dir.adapters" not in (plan_dict.get("source_hits") or [])
    assert not source_coverage_passes(plan_dict, reg)

    wide = _wide_grep_body(ROOT, "router/adapters")
    added2 = register_source_hit(
        plan_dict,
        "dir.adapters",
        success=True,
        content=wide,
        registry=reg,
        tool_name="Grep",
    )
    assert added2
    assert plan_dict.get("source_exploration_stage", {}).get("dir.adapters") == "content"
    assert source_coverage_passes(plan_dict, reg)
    print("test_shallow_dir_grep_does_not_register_hit: OK")


def test_glob_inventory_registers_dir_hit() -> None:
    from reference.source_registry import (
        READ_ONLY_MIN_DIR_GREP_FILES,
        build_source_registry,
        glob_workspace_file_count,
        register_source_hit,
        resolve_root_mapping,
        source_coverage_passes,
    )

    mapping = resolve_root_mapping(str(ROOT))
    reg = build_source_registry(mapping, ["router/adapters"])
    paths = sorted((ROOT / "router" / "adapters").glob("*.py"))
    glob_body = "Result of search in adapters (total {} files):\n".format(len(paths))
    glob_body += "\n".join(str(p) for p in paths)
    assert glob_workspace_file_count(glob_body) >= min(len(paths), READ_ONLY_MIN_DIR_GREP_FILES)
    plan_dict = {
        "router_intent": "read_only_analysis",
        "required_source_ids": ["dir.adapters"],
        "source_hits": [],
        "source_registry": reg.to_dict(),
    }
    added = register_source_hit(
        plan_dict,
        "dir.adapters",
        success=True,
        content=glob_body,
        registry=reg,
        tool_name="Glob",
    )
    assert added
    assert plan_dict.get("source_exploration_stage", {}).get("dir.adapters") == "inventory"
    assert not source_coverage_passes(plan_dict, reg)
    from reference.read_only_explorer import WIDE_CONTENT_GREP

    wide = _wide_grep_body(ROOT, "router/adapters")
    register_source_hit(
        plan_dict,
        "dir.adapters",
        success=True,
        content=wide,
        registry=reg,
        tool_name="Grep",
        pattern=WIDE_CONTENT_GREP,
    )
    assert plan_dict.get("source_exploration_stage", {}).get("dir.adapters") == "content"
    assert source_coverage_passes(plan_dict, reg)
    print("test_glob_inventory_registers_dir_hit: OK")


def test_glob_md_on_code_dir_becomes_grep() -> None:
    from reference.source_registry import RootMapping, build_source_registry, expand_source_tool_call

    mapping = RootMapping(host=str(ROOT), container=str(ROOT / "router"), confidence=0.9, method="test")
    reg = build_source_registry(mapping, ["router/runtime_core"])
    out = expand_source_tool_call(
        "GlobSource",
        {"source_id": "dir.runtime_core", "glob_pattern": "*.md"},
        reg,
    )
    assert out is not None, out
    name, args = out
    assert name == "Grep", name
    assert args.get("pattern") == "."
    print("test_glob_md_on_code_dir_becomes_grep: OK")


def test_build_evidence_answer_agent_plan_no_crash() -> None:
    from reference.plan_state import build_evidence_answer
    from reference.response_guard import build_partial_final_prose

    plan = AgentPlan(
        router_intent="read_only_analysis",
        goal="router 구조",
        source_hits=["dir.adapters", "dir.runtime_core"],
        coverage_hits=["router/adapters", "router/runtime_core"],
        final_ready=True,
    )
    ans = build_evidence_answer(plan, "router 구조")
    assert ans == "", ans
    prose = build_partial_final_prose("router 구조", plan=plan)
    assert prose.strip()
    assert "artifact 분석" not in prose
    assert "Shell 검증" not in prose
    print("test_build_evidence_answer_agent_plan_no_crash: OK")


def test_cursor_looping_flag_forces_final_answer() -> None:
    from reference.plan_state import resolve_agent_phase
    from reference.source_registry import build_source_registry, resolve_root_mapping

    mapping = resolve_root_mapping(str(ROOT))
    preferred = [
        "docs/MODULE_MAP.md",
        "docs/ARCHITECTURE.md",
        "router",
        "router/runtime_core",
        "router/adapters",
        "router/legacy",
        "router/integrations",
    ]
    reg = build_source_registry(mapping, preferred)
    plan_dict = {
        "router_intent": "read_only_analysis",
        "final_ready": True,
        "source_hits": ["dir.adapters", "dir.integrations", "dir.legacy", "dir.runtime_core"],
        "coverage_hits": ["router/adapters", "router/integrations", "router/legacy", "router/runtime_core"],
        "evidence_needed": ["target_coverage"],
        "evidence_collected": ["target_coverage"],
        "preferred_sources": preferred,
        "summary_source_ids": summary_source_ids_for_registry(reg, preferred[:2]),
        "required_source_ids": ["dir.runtime_core", "dir.adapters", "dir.legacy", "dir.integrations"],
        "source_registry": reg.to_dict(),
    }
    _simulate_sufficient_exploration(plan_dict, reg)
    state = SessionState(
        agent_plan=plan_dict,
        final_answer_count=1,
    )
    body = {
        "messages": [
            {"role": "user", "content": "router 구조"},
            {"role": "user", "content": "<system_reminder>Your messages have been flagged as looping."},
        ],
        "tools": [],
    }
    phase = resolve_agent_phase(body, state, "router 구조", "read_only_analysis", True)
    assert phase == "final_answer", phase
    print("test_cursor_looping_flag_forces_final_answer: OK")


def test_judge_llm_max_tokens_defined() -> None:
    from reference import evidence_judge

    assert evidence_judge.JUDGE_LLM_MAX_TOKENS > 0
    print("test_judge_llm_max_tokens_defined: OK")


def test_read_only_resolve_runs_exploration_planner() -> None:
    from reference.plan_state import _resolve_with_agent_plan
    from reference.planner import AgentPlan, apply_policy_constraints

    plan = apply_policy_constraints(
        AgentPlan(goal=STRUCTURE_QUERY),
        STRUCTURE_QUERY,
        router_intent="read_only_analysis",
        workspace=str(ROOT),
    )
    plan.source_hits = ["dir.adapters"]
    plan.source_exploration_stage = {"dir.adapters": "inventory"}
    state = SessionState(agent_plan=plan.to_dict())
    import reference.read_only_explorer as roe
    roe.READ_ONLY_EXPLORER_ENABLED = False
    phase = _resolve_with_agent_plan(
        state,
        plan,
        last_role="tool",
        tool_call_turns=2,
        intent_name="read_only_analysis",
        query=STRUCTURE_QUERY,
    )
    assert phase == "tool_planning", phase
    na = state.agent_plan.get("next_action") or {}
    assert na.get("tool") in ("GrepSource", "GlobSource", "ReadSource", "answer", ""), na
    print("test_read_only_resolve_runs_exploration_planner: OK")


def test_coverage_checker_honors_source_hits() -> None:
    from context_need import ContextNeed
    from coverage_checker import check_coverage

    need = ContextNeed(
        coverage_targets=["dir.adapters", "dir.integrations", "dir.legacy", "dir.runtime_core"],
    )

    class EmptyPack:
        items = []
        missing_targets = []

    class PromptPack:
        system = ""
        current_task = ""
        retrieved = ""
        artifacts = ""
        session_tail = ""
        truncation_markers = []

    report = check_coverage(
        need,
        EmptyPack(),
        PromptPack(),
        source_hits=["dir.adapters", "dir.integrations", "dir.legacy"],
        coverage_hits=["router/adapters", "router/integrations", "router/legacy"],
    )
    assert report.coverage_score >= 0.75, report.coverage_score
    assert "dir.adapters" not in report.missing
    assert "dir.runtime_core" in report.missing
    print("test_coverage_checker_honors_source_hits: OK")


def test_coverage_checker_target_coverage_via_source_hits() -> None:
    from context_need import ContextNeed
    from coverage_checker import check_coverage

    need = ContextNeed(
        coverage_targets=["dir.adapters", "dir.integrations", "dir.legacy", "dir.runtime_core"],
    )

    class Pack:
        items = []
        missing_targets = []

    class PromptPack:
        system = "runtime_core adapters legacy integrations summary"
        current_task = ""
        retrieved = ""
        artifacts = ""
        session_tail = ""
        truncation_markers = []

    report = check_coverage(
        need,
        Pack(),
        PromptPack(),
        evidence_needed=["target_coverage"],
        evidence_collected=["source_hit:dir.runtime_core", "source_hit:dir.adapters"],
        source_hits=["dir.runtime_core", "dir.adapters", "dir.integrations", "dir.legacy"],
        coverage_hits=["router/runtime_core", "router/adapters"],
    )
    assert "evidence:target_coverage" not in report.missing, report.missing
    assert report.complete or report.coverage_score >= 0.85, (report.coverage_score, report.missing)
    print("test_coverage_checker_target_coverage_via_source_hits: OK")


def test_tier_evidence_pack_groups_by_dir() -> None:
    from artifact_excerpt import clear_artifact_excerpt_cache, pack_tier_evidence_for_final
    from legacy.memory_store import Artifact

    clear_artifact_excerpt_cache()
    arts = [
        Artifact(
            artifact_id="a1",
            req_id="r",
            delta_id="d",
            type="tool_result",
            name="Grep",
            path="/home/yunahe/ai-runtime/cursor-local-llm/router/runtime_core",
            chars=5000,
            prompt_excerpt="[llm summary grep] runtime_core policy modules",
        ),
        Artifact(
            artifact_id="a2",
            req_id="r",
            delta_id="d",
            type="tool_result",
            name="Grep",
            path="/home/yunahe/ai-runtime/cursor-local-llm/router/adapters",
            chars=4000,
            prompt_excerpt="[llm summary grep] gateway adapter",
        ),
    ]
    block, digests = pack_tier_evidence_for_final(arts, 8000, phase="final_answer")
    assert digests.get("runtime_core") and digests.get("adapters"), digests
    assert "tier:" in block
    print("test_tier_evidence_pack_groups_by_dir: OK")


def test_filter_redundant_allows_grep_after_glob_hit() -> None:
    from reference.agent_exec import synthetic_tool_calls_response
    from reference.planner import AgentPlan
    from reference.source_registry import build_source_registry, resolve_root_mapping
    from reference.source_tools import filter_redundant_source_tool_calls

    mapping = resolve_root_mapping(str(ROOT))
    reg = build_source_registry(mapping, ["router/runtime_core"])
    plan = AgentPlan(
        router_intent="read_only_analysis",
        source_hits=["dir.runtime_core"],
        source_exploration_stage={"dir.runtime_core": "inventory"},
        exploration_actions_tried=["glob:dir.runtime_core:*.py"],
        source_registry=reg.to_dict(),
    )
    base = {"choices": [{"message": {"role": "assistant", "content": ""}}]}
    resp = synthetic_tool_calls_response(
        base,
        [("GrepSource", {"source_id": "dir.runtime_core", "pattern": 'class |def |"""'})],
    )
    out, removed = filter_redundant_source_tool_calls(resp, plan)
    assert removed == 0, removed
    tcs = out["choices"][0]["message"].get("tool_calls") or []
    assert len(tcs) == 1
    print("test_filter_redundant_allows_grep_after_glob_hit: OK")


def test_explorer_skips_repeated_grep_action() -> None:
    from reference.read_only_explorer import (
        WIDE_CONTENT_GREP,
        exploration_action_sig,
        plan_read_only_exploration,
    )
    from reference.planner import AgentPlan, apply_policy_constraints

    q = STRUCTURE_QUERY
    plan = apply_policy_constraints(
        AgentPlan(goal=q),
        q,
        router_intent="read_only_analysis",
        workspace=str(ROOT),
    )
    sid = "dir.runtime_core"
    if sid not in (plan.required_source_ids or []):
        sid = (plan.required_source_ids or ["dir.adapters"])[0]
    plan.source_exploration_stage = {sid: "inventory"}
    plan.exploration_actions_tried = [
        exploration_action_sig("GrepSource", sid, pattern="."),
    ]
    os.environ["READ_ONLY_EXPLORER_ENABLED"] = "0"
    try:
        decision = plan_read_only_exploration(None, plan, q)
    finally:
        os.environ.pop("READ_ONLY_EXPLORER_ENABLED", None)
    sig = exploration_action_sig(decision.tool, decision.source_id, pattern=decision.pattern)
    assert sig != exploration_action_sig("GrepSource", sid, pattern="."), (decision.tool, decision.pattern)
    if decision.tool == "GrepSource" and decision.source_id == sid:
        assert decision.pattern != ".", decision.pattern
    print("test_explorer_skips_repeated_grep_action: OK")


def test_exploration_context_includes_digests_and_tried() -> None:
    from reference.read_only_explorer import build_exploration_context, exploration_action_sig
    from reference.planner import AgentPlan, apply_policy_constraints

    q = STRUCTURE_QUERY
    plan = apply_policy_constraints(
        AgentPlan(goal=q),
        q,
        router_intent="read_only_analysis",
        workspace=str(ROOT),
    )
    plan.exploration_actions_tried = [exploration_action_sig("GlobSource", "dir.adapters", glob_pattern="*.py")]
    plan.source_digests = {"dir.adapters": "- adapters/gateway.py: HTTP gateway"}
    ctx = build_exploration_context(None, plan, q)
    assert "exploration_actions_tried" in ctx
    assert ctx["exploration_actions_tried"]
    assert ctx.get("source_digests") or plan.source_digests
    assert "exploration_checklist" in ctx
    assert isinstance(ctx["exploration_checklist"], dict)
    print("test_exploration_context_includes_digests_and_tried: OK")


def test_grep_pattern_canonicalization_blocks_repeat() -> None:
    from reference.read_only_explorer import BOUNDARY_GREP, exploration_action_sig

    a = exploration_action_sig("GrepSource", "dir.runtime_core", pattern="import|from.*import")
    b = exploration_action_sig("GrepSource", "dir.runtime_core", pattern=BOUNDARY_GREP)
    assert a == b, (a, b)
    print("test_grep_pattern_canonicalization_blocks_repeat: OK")


def test_mark_final_ready_requires_exploration_depth() -> None:
    from reference.planner import AgentPlan, apply_policy_constraints, mark_final_ready
    from reference.source_registry import register_source_hit

    q = STRUCTURE_QUERY
    plan = apply_policy_constraints(
        AgentPlan(goal=q),
        q,
        router_intent="read_only_analysis",
        workspace=str(ROOT),
    )
    mapping = resolve_root_mapping(str(ROOT))
    reg = build_source_registry(mapping, plan.preferred_sources)
    plan.source_registry = reg.to_dict()
    plan_dict = plan.to_dict()
    for sid in ("dir.runtime_core", "dir.adapters", "dir.integrations", "dir.legacy"):
        register_source_hit(plan_dict, sid, success=True, content="x" * 200, registry=reg, tool_name="Glob")
    plan.source_hits = list(plan_dict.get("source_hits") or [])
    plan.coverage_hits = list(plan_dict.get("coverage_hits") or [])
    mark_final_ready(plan, query=q, project_root=str(ROOT))
    assert not plan.final_ready, "must not final_ready without exploration depth"
    print("test_mark_final_ready_requires_exploration_depth: OK")


def test_exploration_checklist_advances_tiers() -> None:
    from reference.read_only_explorer import build_exploration_checklist, exploration_action_sig
    from reference.planner import AgentPlan, apply_policy_constraints

    q = STRUCTURE_QUERY
    plan = apply_policy_constraints(
        AgentPlan(goal=q),
        q,
        router_intent="read_only_analysis",
        workspace=str(ROOT),
    )
    reg = build_source_registry(resolve_root_mapping(str(ROOT)), plan.preferred_sources)
    plan_dict = plan.to_dict()
    sid = "dir.runtime_core"
    plan_dict["exploration_actions_tried"] = [
        exploration_action_sig("GlobSource", sid, glob_pattern="*.py"),
        exploration_action_sig("GrepSource", sid, pattern='class |def |"""'),
    ]
    plan_dict["source_exploration_stage"] = {sid: "content"}
    checklist = build_exploration_checklist(plan_dict, reg)
    assert checklist.get(f"glob:{sid}") == "done"
    assert checklist.get(f"grep_content:{sid}") == "done"
    assert checklist.get(f"grep_boundary:{sid}") == "pending"
    print("test_exploration_checklist_advances_tiers: OK")


def test_exploration_records_pattern_from_next_action() -> None:
    from reference.planner import AgentPlan, apply_policy_constraints, update_plan_after_tool
    from reference.read_only_explorer import BOUNDARY_GREP, exploration_action_sig
    from adapters.memory import SessionState

    q = STRUCTURE_QUERY
    plan = apply_policy_constraints(
        AgentPlan(goal=q),
        q,
        router_intent="read_only_analysis",
        workspace=str(ROOT),
    )
    plan.next_action = {
        "tool": "GrepSource",
        "source_id": "dir.adapters",
        "pattern": BOUNDARY_GREP,
    }
    state = SessionState()
    update_plan_after_tool(
        plan,
        state,
        tool_name="Grep",
        args={"path": str(ROOT / "router" / "adapters"), "_source_id": "dir.adapters"},
        result_text="<workspace_result workspace_path='/home/yunahe'>\nrouter/adapters/gateway.py\n</workspace_result>",
        success=True,
    )
    sig = exploration_action_sig("GrepSource", "dir.adapters", pattern=BOUNDARY_GREP)
    assert sig in (plan.exploration_actions_tried or []), plan.exploration_actions_tried
    print("test_exploration_records_pattern_from_next_action: OK")


def main() -> int:
    tests = [
        test_read_only_analysis_no_shell_by_default,
        test_project_structure_discovered_from_query,
        test_home_directory_not_project_root,
        test_answer_action_requires_target_coverage,
        test_final_ready_after_tool_results,
        test_partial_module_coverage_does_not_pass,
        test_full_structure_coverage_passes,
        test_target_coverage_evidence_not_string_only,
        test_read_only_phase_uses_coverage_not_string_evidence,
        test_read_only_docs_sufficient_passes,
        test_no_empty_response,
        test_read_only_injects_tools_without_cursor_tools,
        test_json_tool_calls_parsed_from_content,
        test_json_tool_name_field_parsed,
        test_empty_glob_does_not_register_source_hit,
        test_read_only_no_early_final_after_empty_globs,
        test_read_only_explorer_static_ladder,
        test_wide_grep_pattern_forces_on_dir,
        test_shallow_dir_grep_does_not_register_hit,
        test_glob_inventory_registers_dir_hit,
        test_glob_md_on_code_dir_becomes_grep,
        test_build_evidence_answer_agent_plan_no_crash,
        test_cursor_looping_flag_forces_final_answer,
        test_judge_llm_max_tokens_defined,
        test_read_only_resolve_runs_exploration_planner,
        test_coverage_checker_honors_source_hits,
        test_coverage_checker_target_coverage_via_source_hits,
        test_tier_evidence_pack_groups_by_dir,
        test_filter_redundant_allows_grep_after_glob_hit,
        test_explorer_skips_repeated_grep_action,
        test_exploration_context_includes_digests_and_tried,
        test_grep_pattern_canonicalization_blocks_repeat,
        test_mark_final_ready_requires_exploration_depth,
        test_exploration_checklist_advances_tiers,
        test_exploration_records_pattern_from_next_action,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}", file=sys.stderr)
    print(json.dumps({"passed": len(tests) - failed, "failed": failed, "total": len(tests)}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
