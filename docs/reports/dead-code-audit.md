# Dead Code Audit

> Generated: 2026-06-22 03:17:29 UTC
> Entrypoints: 5

## Summary

| Classification | Modules |
|----------------|--------:|
| active_hot_path | 68 |
| active_test_only | 0 |
| active_cli_only | 0 |
| legacy_fallback | 6 |
| planned | 0 |
| dead_candidate | 75 |
| unknown_needs_review | 3 |

## Dead candidates (sample)

| Module | Path | Imported by |
|--------|------|-------------|
| `adapters.mcp` | `router/adapters/mcp.py` | - |
| `integrations.langgraph_memory` | `router/integrations/langgraph_memory.py` | - |
| `observability` | `router/observability/__init__.py` | - |
| `runtime_core.evidence_cluster` | `router/runtime_core/evidence_cluster.py` | - |
| `runtime_core.indexing_helpers` | `router/runtime_core/indexing_helpers.py` | - |
| `runtime_core.memory_hierarchy` | `router/runtime_core/memory_hierarchy.py` | - |
| `runtime_core.memory_policy` | `router/runtime_core/memory_policy.py` | - |
| `runtime_core.runtime_events` | `router/runtime_core/runtime_events.py` | - |
| `runtime_core.scheduler_contract` | `router/runtime_core/scheduler_contract.py` | - |
| `runtime_kernel.runtime_paths` | `router/runtime_kernel/runtime_paths.py` | - |
| `runtime_kernel.self_model` | `router/runtime_kernel/self_model.py` | - |
| `scripts.analyze-conversation-flow` | `scripts/analyze-conversation-flow.py` | - |
| `scripts.analyze-cursor-captures` | `scripts/analyze-cursor-captures.py` | - |
| `scripts.audit-dead-code` | `scripts/audit-dead-code.py` | - |
| `scripts.audit-foreign-artifacts` | `scripts/audit-foreign-artifacts.py` | - |
| `scripts.audit-repo-inventory` | `scripts/audit-repo-inventory.py` | - |
| `scripts.audit-runtime-reachability` | `scripts/audit-runtime-reachability.py` | - |
| `scripts.benchmark-agent-deadend-regression` | `scripts/benchmark-agent-deadend-regression.py` | - |
| `scripts.benchmark-cursor-agent` | `scripts/benchmark-cursor-agent.py` | - |
| `scripts.benchmark-dynamic-budget-matrix` | `scripts/benchmark-dynamic-budget-matrix.py` | - |
| `scripts.benchmark-dynamic-budget` | `scripts/benchmark-dynamic-budget.py` | - |
| `scripts.benchmark-gateway-swap` | `scripts/benchmark-gateway-swap.py` | - |
| `scripts.benchmark-memory-backend-swap` | `scripts/benchmark-memory-backend-swap.py` | - |
| `scripts.benchmark-memory-hierarchy` | `scripts/benchmark-memory-hierarchy.py` | - |
| `scripts.benchmark-read-only-analysis-regression` | `scripts/benchmark-read-only-analysis-regression.py` | - |
| `scripts.benchmark-recovery-e2e` | `scripts/benchmark-recovery-e2e.py` | - |
| `scripts.benchmark-repeated-read-avoidance` | `scripts/benchmark-repeated-read-avoidance.py` | - |
| `scripts.benchmark-retriever-swap` | `scripts/benchmark-retriever-swap.py` | - |
| `scripts.benchmark-route-backend-regression` | `scripts/benchmark-route-backend-regression.py` | - |
| `scripts.benchmark-router-live` | `scripts/benchmark-router-live.py` | - |
| `scripts.benchmark-runtime-score` | `scripts/benchmark-runtime-score.py` | - |
| `scripts.benchmark-runtime` | `scripts/benchmark-runtime.py` | - |
| `scripts.benchmark-source-registry-regression` | `scripts/benchmark-source-registry-regression.py` | - |
| `scripts.benchmark-thinking-runtime` | `scripts/benchmark-thinking-runtime.py` | - |
| `scripts.benchmark-vector-retrieval-e2e` | `scripts/benchmark-vector-retrieval-e2e.py` | - |
| `scripts.check-architecture-boundary` | `scripts/check-architecture-boundary.py` | - |
| `scripts.clean-foreign-artifacts` | `scripts/clean-foreign-artifacts.py` | - |
| `scripts.generate-dependency-graph` | `scripts/generate-dependency-graph.py` | - |
| `scripts.generate-file-tree` | `scripts/generate-file-tree.py` | - |
| `scripts.generate-project-structure` | `scripts/generate-project-structure.py` | - |

## Legacy fallback

| Module | Env refs | Reachable |
|--------|----------|-----------|
| `router/legacy/__init__.py` | LEGACY_OPTIMIZER | True |
| `router/legacy/agent_runs.py` | AGENT_RUNS_DIR, AGENT_RUNS_ENABLED, AGENT_RUNS_MAX_EVENTS, AGENT_RUNS_MAX_RUNS | True |
| `router/legacy/context_optimizer.py` | CONTEXT_OPTIMIZER, OPTIMIZER_ERROR_KEEP_CHARS, OPTIMIZER_MODE, OPTIMIZER_PREVIEW_CHARS | False |
| `router/legacy/memory_store.py` | CONTEXT_CACHE_DIR, MEMORY_STORE | True |
| `router/legacy/retriever.py` | - | False |
| `router/legacy/runtime_optimizer.py` | RUNTIME_OPTIMIZER, RUNTIME_OPTIMIZER_MIN_TOOL_CHARS, RUNTIME_OPTIMIZER_SHRINK_STEP, RUNTIME_OPTIMIZER_STATE | False |

## Unknown (needs review)

- `router/adapters/gateway.py` ← scripts.benchmark-gateway-swap
- `router/observability/trace_ssot.py` ← observability
- `router/runtime_core/evidence_keys.py` ← runtime_core.evidence_cluster

## Known legacy archive candidates

| File | Notes |
|------|-------|
| `legacy/context_optimizer.py` | CONTEXT_OPTIMIZER path |
| `legacy/runtime_optimizer.py` | RUNTIME_OPTIMIZER |
| thin adapter wrappers | verify adapters/* usage |
| old explorer/read_only code | superseded by read_only_explorer |

*Regenerate: `python3 scripts/audit-dead-code.py`*

See also: `docs/reports/legacy-archive-plan.md`
