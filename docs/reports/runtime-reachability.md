# Runtime Reachability Audit

> Generated: 2026-06-22 03:21:36 UTC

## Summary

| Usage class | Count |
|-------------|------:|
| active_hot_path | 683 |
| active_guard | 45 |
| active_optional | 3 |
| active_cli_only | 0 |
| active_test_only | 0 |
| legacy_fallback | 4 |
| imported_but_dead_branch | 0 |
| dead_candidate | 3 |
| unknown_needs_review | 7 |

## Observed at runtime (sample)

| Module | Symbol | Class |
|--------|--------|-------|
| `adapters` | `<module>` | active_hot_path |
| `adapters` | `<module>` | active_hot_path |
| `adapters.memory` | `memory_backend_name` | active_hot_path |
| `adapters.memory` | `get_memory_backend_metrics` | active_hot_path |
| `adapters.memory` | `load_session_state` | active_hot_path |
| `adapters.memory` | `load_state` | active_hot_path |
| `adapters.memory` | `save_state` | active_hot_path |
| `adapters.memory` | `save_turn_delta` | active_hot_path |
| `adapters.memory` | `save_artifact` | active_hot_path |
| `adapters.memory` | `save_tool_result` | active_hot_path |
| `adapters.memory` | `query_memory` | active_hot_path |
| `adapters.memory` | `compact_memory` | active_hot_path |
| `adapters.memory` | `build_working_set` | active_hot_path |
| `adapters.memory` | `collect_hierarchy_snapshot` | active_hot_path |
| `adapters.memory` | `<module>` | active_hot_path |
| `adapters.observe` | `emit_observation` | active_hot_path |
| `adapters.observe` | `<module>` | active_hot_path |
| `adapters.retrieval` | `retrieve_for_need` | active_hot_path |
| `adapters.retrieval` | `<module>` | active_hot_path |
| `adapters.trace` | `set_trace_context` | active_hot_path |
| `adapters.trace` | `get_trace_context` | active_hot_path |
| `adapters.trace` | `merge_trace_context` | active_hot_path |
| `adapters.trace` | `emit_runtime_event` | active_hot_path |
| `adapters.trace` | `<module>` | active_hot_path |
| `agent_brain.llm_planner` | `llm_planner_shadow_enabled` | active_optional |
| `agent_brain.llm_planner` | `propose_llm_shadow_decision` | active_optional |
| `agent_brain.llm_planner` | `<module>` | active_optional |
| `agent_brain.planner_shadow` | `planner_shadow_enabled` | active_hot_path |
| `agent_brain.planner_shadow` | `rule_decision_from_plan` | active_hot_path |
| `agent_brain.planner_shadow` | `propose_shadow_decision` | active_hot_path |
| `agent_brain.planner_shadow` | `compare_shadow_decisions` | active_hot_path |
| `agent_brain.planner_shadow` | `compare_triple_decisions` | active_hot_path |
| `agent_brain.planner_shadow` | `run_planner_shadow` | active_hot_path |
| `agent_brain.planner_shadow` | `run_planner_shadow_if_enabled` | active_hot_path |
| `agent_brain.planner_shadow` | `<module>` | active_hot_path |
| `artifact_analyzer` | `analyze_content` | active_hot_path |
| `artifact_analyzer` | `extract_compose_port_evidence` | active_hot_path |
| `artifact_analyzer` | `format_analysis_compact` | active_hot_path |
| `artifact_analyzer` | `html_validation_command` | active_hot_path |
| `artifact_analyzer` | `__init__` | active_hot_path |

## Imported but dead branch

| Module | Env | Recommendation |
|--------|-----|----------------|

## Dead candidates (archive later)

| Module | Path |
|--------|------|
| `adapters.mcp` | `router/adapters/mcp.py` |

*Regenerate:*
```bash
python3 scripts/audit-runtime-reachability.py --static
python3 scripts/audit-runtime-reachability.py --profile
python3 scripts/audit-runtime-reachability.py --merge
```
