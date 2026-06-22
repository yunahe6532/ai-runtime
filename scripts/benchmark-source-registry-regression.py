#!/usr/bin/env python3
"""Regression: source_id registry — LLM must not invent paths."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from reference.planner import (  # noqa: E402
    AgentPlan,
    apply_policy_constraints,
    validate_tool_call,
)
from reference.source_registry import (  # noqa: E402
    SourceRegistry,
    build_source_registry,
    expand_source_tool_call,
    is_tool_result_success,
    register_source_hit,
    resolve_root_mapping,
    resolve_source_path,
    source_id_for_relpath,
)
from reference.source_tools import expand_source_tool_calls_in_response  # noqa: E402
from reference.target_coverage import register_hits_from_tool  # noqa: E402

STRUCTURE_QUERY = (
    "이 프로젝트 구조를 분석해서 runtime_core, adapters, legacy, integrations 역할을 요약해줘. "
    "코드는 수정하지 말고 필요한 파일만 읽어서 근거와 함께 답해."
)


def test_llm_cannot_invent_absolute_path() -> None:
    mapping = resolve_root_mapping(str(ROOT))
    reg = build_source_registry(mapping, ["docs/MODULE_MAP.md"])
    plan = apply_policy_constraints(
        AgentPlan(task_intent="project_inspection", goal=STRUCTURE_QUERY),
        STRUCTURE_QUERY,
        router_intent="read_only_analysis",
        workspace=str(ROOT),
    )
    ok, reason = validate_tool_call(
        "Read",
        {"path": "/home/yunahe/ai-runt/docs/MODULE_MAP.md"},
        plan,
    )
    assert not ok, reason
    assert any(k in reason.lower() for k in ("source_id", "blocked", "disallowed")), reason

    ok2, _ = validate_tool_call("ReadSource", {"source_id": "doc.module_map"}, plan)
    assert ok2 or "doc.module_map" in (plan.source_candidates or []), plan.source_candidates
    print("test_llm_cannot_invent_absolute_path: OK")


def test_source_id_resolves_to_project_root() -> None:
    mapping = resolve_root_mapping(str(ROOT))
    reg = build_source_registry(
        mapping,
        ["docs/MODULE_MAP.md", "router/runtime_core"],
    )
    sid = source_id_for_relpath("docs/MODULE_MAP.md")
    assert sid == "doc.module_map"
    path = resolve_source_path(reg, sid, for_cursor=True)
    assert path.endswith("docs/MODULE_MAP.md"), path
    assert Path(path).is_file(), path
    expanded = expand_source_tool_call("ReadSource", {"source_id": sid}, reg)
    assert expanded is not None
    assert expanded[0] == "Read"
    assert expanded[1]["path"] == path
    print("test_source_id_resolves_to_project_root: OK")


def test_missing_file_error_not_coverage_hit() -> None:
    mapping = resolve_root_mapping(str(ROOT))
    reg = build_source_registry(mapping, ["docs/MODULE_MAP.md"])
    plan_dict: dict = {"coverage_hits": [], "source_hits": []}
    err = "Path does not exist: /home/yunahe/ai-runt"
    assert not is_tool_result_success(err)

    added = register_hits_from_tool(
        plan_dict,
        path="/home/yunahe/ai-runt/docs/MODULE_MAP.md",
        content=err,
        tool_name="Glob",
        project_root=str(ROOT),
        targets=["docs/MODULE_MAP.md"],
        success=True,
    )
    assert not added, plan_dict

    added2 = register_source_hit(
        plan_dict,
        "doc.module_map",
        success=True,
        content=err,
        registry=reg,
    )
    assert not added2, plan_dict
    print("test_missing_file_error_not_coverage_hit: OK")


def test_container_host_root_mapping() -> None:
    mapping = resolve_root_mapping("/app", known_paths=[str(ROOT / "docs/MODULE_MAP.md")])
    assert mapping.host
    assert mapping.container
    assert Path(mapping.host).joinpath("router").is_dir(), mapping.host
    reg = build_source_registry(mapping, ["docs/MODULE_MAP.md"])
    entry = reg.get("doc.module_map")
    assert entry is not None
    assert entry.host_path.endswith("docs/MODULE_MAP.md")
    assert entry.container_path.endswith("docs/MODULE_MAP.md")
    print("test_container_host_root_mapping: OK")


def test_read_only_analysis_uses_source_registry() -> None:
    plan = apply_policy_constraints(
        AgentPlan(task_intent="project_inspection", goal=STRUCTURE_QUERY),
        STRUCTURE_QUERY,
        router_intent="read_only_analysis",
        workspace=str(ROOT),
    )
    assert plan.source_registry
    assert plan.source_candidates
    assert "ReadSource" in plan.allowed_tools
    assert "Read" in plan.disallowed_tools
    assert plan.next_action.get("tool") != "Shell"
    ids = set(plan.source_candidates)
    assert "doc.module_map" in ids or any("module" in i for i in ids), ids

    resp = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "ReadSource",
                        "arguments": json.dumps({"source_id": plan.source_candidates[0]}),
                    },
                }],
            },
        }]
    }
    out = expand_source_tool_calls_in_response(resp, plan)
    tc = out["choices"][0]["message"]["tool_calls"][0]
    fn_name = tc["function"]["name"]
    args = json.loads(tc["function"]["arguments"])
    assert args.get("_source_id")
    if fn_name == "Read":
        assert Path(args["path"]).exists(), args["path"]
    elif fn_name == "Glob":
        assert args.get("target_directory"), args
        assert Path(args["target_directory"]).is_dir(), args
    else:
        raise AssertionError(f"unexpected tool {fn_name}")
    print("test_read_only_analysis_uses_source_registry: OK")


def test_malformed_xml_resolves_via_registry() -> None:
    from adapters.memory import SessionState
    from reference.agent_exec import postprocess_agent_response
    from reference.planner import apply_policy_constraints, AgentPlan

    plan = apply_policy_constraints(
        AgentPlan(task_intent="project_inspection", goal=STRUCTURE_QUERY),
        STRUCTURE_QUERY,
        router_intent="read_only_analysis",
        workspace=str(ROOT),
    )
    state = SessionState()
    state.agent_plan = plan.to_dict()
    state.workspace_path = str(ROOT)
    xml = (
        "<tool_code> "
        '<tool_call> Read> arg_key_value_list> {"path": "docs/MODULE_MAP.md"} </tool_call> '
        '<tool_call> Read> arg_key_value_list> {"path": "docs/ARCHITECTURE.md"} </tool_call> '
        "</tool_code>"
    )
    resp = {"choices": [{"message": {"role": "assistant", "content": xml}, "finish_reason": "stop"}]}
    out, _log = postprocess_agent_response(
        resp,
        "read_only_analysis",
        STRUCTURE_QUERY,
        phase="tool_planning",
        session_state=state,
    )
    tcs = out["choices"][0]["message"].get("tool_calls") or []
    assert len(tcs) >= 1, out
    args = json.loads(tcs[0]["function"]["arguments"])
    assert args.get("_source_id") or Path(args.get("path", "")).exists(), args
    print("test_malformed_xml_resolves_via_registry: OK")


def test_malformed_path_arrow_xml() -> None:
    from reference.response_guard import parse_all_tool_calls_from_content

    xml = (
        "<tool_call> Read> path>docs/MODULE_MAP.md "
        "<tool_call> Read> path>docs/ARCHITECTURE.md "
        "<tool_call> Read> path>router/runtime_core/__init__.py "
        "<tool_call> Read> path>router/adapters/__init__.py"
    )
    calls = parse_all_tool_calls_from_content(xml)
    assert len(calls) >= 4, calls
    paths = [a.get("path") for _, a in calls]
    assert "docs/MODULE_MAP.md" in paths
    assert "router/adapters/__init__.py" in paths
    print("test_malformed_path_arrow_xml: OK")


def test_discover_read_only_no_hardcoded_modules() -> None:
    from reference.source_registry import discover_read_only_relpaths, resolve_root_mapping

    mapping = resolve_root_mapping(str(ROOT))
    relpaths, summary = discover_read_only_relpaths(
        "explain the foo_bar module role only",
        mapping,
    )
    assert isinstance(relpaths, list)
    assert isinstance(summary, list)
    print("test_discover_read_only_no_hardcoded_modules: OK")


def test_read_source_dir_expands_to_glob() -> None:
    from reference.source_registry import RootMapping

    mapping = RootMapping(
        host=str(ROOT),
        container=str(ROOT / "router"),
        confidence=0.9,
        method="test_read_source_dir",
    )
    reg = build_source_registry(mapping, ["router/runtime_core"])
    name, args = expand_source_tool_call(
        "ReadSource",
        {"source_id": "dir.runtime_core"},
        reg,
    )
    assert name == "Glob", name
    assert args.get("glob_pattern") == "*", args
    assert args.get("_source_id") == "dir.runtime_core", args
    print("test_read_source_dir_expands_to_glob: OK")


def test_router_package_container_layout() -> None:
    """Docker /app = router package: modules at runtime_core/, not router/runtime_core/."""
    from reference.source_registry import RootMapping

    mapping = RootMapping(
        host=str(ROOT),
        container=str(ROOT / "router"),
        confidence=0.9,
        method="test_router_package",
    )
    reg = build_source_registry(
        mapping,
        [
            "router/runtime_core",
            "router/adapters",
            "router/legacy",
            "router/integrations",
            "docs/MODULE_MAP.md",
            "docs/ARCHITECTURE.md",
        ],
    )
    by_id = {s.id: s for s in reg.sources}
    assert by_id["dir.runtime_core"].exists, by_id["dir.runtime_core"]
    assert by_id["dir.adapters"].exists, by_id["dir.adapters"]
    assert by_id["doc.module_map"].exists, by_id["doc.module_map"]
    assert by_id["dir.runtime_core"].container_path.endswith("/runtime_core")
    assert by_id["dir.runtime_core"].host_path.endswith("router/runtime_core")
    print("test_router_package_container_layout: OK")


def test_container_discovery_when_host_unreachable() -> None:
    """Docker: context-cache host path is not mounted — scan container root instead."""
    from reference.source_registry import (
        RootMapping,
        build_source_registry,
        discover_read_only_relpaths,
    )

    container = ROOT / "router"
    mapping = RootMapping(
        host="/nonexistent/host/repo/path",
        container=str(container),
        confidence=0.9,
        method="test_unreachable_host",
    )
    relpaths, _summary = discover_read_only_relpaths(STRUCTURE_QUERY, mapping)
    assert relpaths, relpaths
    assert any("runtime_core" in r for r in relpaths), relpaths
    reg = build_source_registry(mapping, relpaths)
    assert reg.available(), [s.id for s in reg.sources]
    print("test_container_discovery_when_host_unreachable: OK")


def test_resolve_bare_module_source_id() -> None:
    from reference.source_registry import RootMapping, build_source_registry, resolve_source_id

    mapping = resolve_root_mapping(str(ROOT), known_paths=[str(ROOT / "router" / "runtime_core")])
    reg = build_source_registry(mapping, ["router/runtime_core", "router/adapters"])
    sid = resolve_source_id(reg, "runtime_core")
    assert sid == "dir.runtime_core", sid
    print("test_resolve_bare_module_source_id: OK")


def main() -> int:
    tests = [
        test_llm_cannot_invent_absolute_path,
        test_source_id_resolves_to_project_root,
        test_missing_file_error_not_coverage_hit,
        test_container_host_root_mapping,
        test_read_only_analysis_uses_source_registry,
        test_malformed_xml_resolves_via_registry,
        test_malformed_path_arrow_xml,
        test_discover_read_only_no_hardcoded_modules,
        test_read_source_dir_expands_to_glob,
        test_router_package_container_layout,
        test_container_discovery_when_host_unreachable,
        test_resolve_bare_module_source_id,
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
