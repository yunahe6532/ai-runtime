# Project Structure

> Generated: 2026-06-22 03:17:28 UTC
> Root: `/home/yunahe/ai-runtime/cursor-local-llm`

Source-centric logical tree (vendor, tmp, cache, node_modules excluded).

## Included Logical Tree

```
cursor-local-llm/
├── .github/
│   └── workflows/
├── config/
│   └── runtime_self_model.yaml
├── configs/
│   ├── langfuse-local.env
│   ├── litellm-gateway-live.yaml
│   ├── model-map.env
│   ├── model-profiles.env
│   └── models.manifest.json
├── docs/
│   ├── archive/
│   │   └── VISION-os-era-2026-06.md
│   ├── assets/
│   │   ├── context-runtime-1page.mmd
│   │   ├── dependency-after.mmd
│   │   ├── dependency-before.mmd
│   │   └── memory-hierarchy.mmd
│   ├── reports/
│   │   ├── archive-candidates.md
│   │   ├── dead-code-audit.md
│   │   ├── deprecated-branches.md
│   │   ├── deprecated-env.md
│   │   ├── foreign-artifacts-audit.md
│   │   ├── foreign-artifacts-cleanup.md
│   │   ├── legacy-archive-plan.md
│   │   ├── repo-inventory.md
│   │   └── runtime-reachability.md
│   ├── ARCHITECTURE.html
│   ├── ARCHITECTURE.md
│   ├── BENCHMARK.html
│   ├── BENCHMARK.md
│   ├── dependency-before-after.md
│   ├── INTEGRATIONS.html
│   ├── INTEGRATIONS.md
│   ├── MODULE_MAP.html
│   ├── MODULE_MAP.md
│   ├── PROJECT_STRUCTURE.md
│   ├── REFACTOR.md
│   ├── VISION.html
│   ├── VISION.md
│   ├── VISION.pdf
│   ├── WORKSPACE_LAYOUT.html
│   └── WORKSPACE_LAYOUT.md
├── router/
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── gateway.py
│   │   ├── langgraph.py
│   │   ├── mcp.py
│   │   ├── memory.py
│   │   ├── observe.py
│   │   ├── retrieval.py
│   │   └── trace.py
│   ├── agent_brain/
│   │   ├── __init__.py
│   │   ├── llm_planner.py
│   │   ├── planner_contract.py
│   │   ├── planner_shadow.py
│   │   ├── promotion_gate.py
│   │   └── runtime_state.py
│   ├── integrations/
│   │   ├── __init__.py
│   │   ├── flow_tracing.py
│   │   ├── langfuse.py
│   │   ├── langgraph_memory.py
│   │   ├── llamaindex.py
│   │   └── otel.py
│   ├── legacy/
│   │   ├── __init__.py
│   │   ├── agent_runs.py
│   │   ├── context_optimizer.py
│   │   ├── memory_store.py
│   │   ├── retriever.py
│   │   └── runtime_optimizer.py
│   ├── observability/
│   │   ├── __init__.py
│   │   └── trace_ssot.py
│   ├── reference/
│   │   ├── __init__.py
│   │   ├── agent_exec.py
│   │   ├── answer_tokens.py
│   │   ├── evidence_extractors.py
│   │   ├── evidence_judge.py
│   │   ├── evidence_store.py
│   │   ├── loop_guard.py
│   │   ├── plan_state.py
│   │   ├── planner.py
│   │   ├── project_root.py
│   │   ├── read_guard.py
│   │   ├── read_only_explorer.py
│   │   ├── response_guard.py
│   │   ├── source_registry.py
│   │   ├── source_tools.py
│   │   └── target_coverage.py
│   ├── runtime_core/
│   │   ├── __init__.py
│   │   ├── evidence_cluster.py
│   │   ├── evidence_keys.py
│   │   ├── indexing_helpers.py
│   │   ├── memory_hierarchy.py
│   │   ├── memory_policy.py
│   │   ├── prompt_enforcer.py
│   │   ├── runtime_events.py
│   │   └── scheduler_contract.py
│   ├── runtime_kernel/
│   │   ├── __init__.py
│   │   ├── constants.py
│   │   ├── evidence_anchor.py
│   │   ├── evidence_ingest.py
│   │   ├── final_report.py
│   │   ├── intent.py
│   │   ├── memory_limits.py
│   │   ├── phase.py
│   │   ├── project_index.py
│   │   ├── runtime_paths.py
│   │   ├── runtime_state.py
│   │   ├── self_model.py
│   │   ├── task_journal.py
│   │   └── working_set.py
│   ├── .dockerignore
│   ├── artifact_analyzer.py
│   ├── artifact_excerpt.py
│   ├── capture.py
│   ├── chat_fast.py
│   ├── context_budget.py
│   ├── context_cache.py
│   ├── context_need.py
│   ├── coverage_checker.py
│   ├── cursor_reasoning.py
│   ├── Dockerfile
│   ├── dynamic_context_scheduler.py
│   ├── explorer_trace.py
│   ├── failed_action.py
│   ├── intent_router.py
│   ├── main.py
│   ├── message_index.py
│   ├── prompt_builder.py
│   ├── qwen_request.py
│   ├── recovery_scheduler.py
│   ├── requirements-integrations.txt
│   ├── runtime_inspector.py
│   ├── runtime_turn_log.py
│   └── vl_pass.py
├── scripts/
│   ├── pdf-export/
│   │   ├── export-md-html.mjs
│   │   ├── export-md-pdf.mjs
│   │   ├── mermaid-config.json
│   │   ├── package-lock.json
│   │   └── package.json
│   ├── analyze-conversation-flow.py
│   ├── analyze-cursor-captures.py
│   ├── audit-dead-code.py
│   ├── audit-foreign-artifacts.py
│   ├── audit-repo-inventory.py
│   ├── audit-runtime-reachability.py
│   ├── benchmark-agent-deadend-regression.py
│   ├── benchmark-coder-fast-vs-vl.sh
│   ├── benchmark-context-grid.sh
│   ├── benchmark-cursor-agent.py
│   ├── benchmark-dram-200k.sh
│   ├── benchmark-dynamic-budget-matrix.py
│   ├── benchmark-dynamic-budget.py
│   ├── benchmark-gateway-live.sh
│   ├── benchmark-gateway-swap.py
│   ├── benchmark-kv-profiles.sh
│   ├── benchmark-longrun-cursor.sh
│   ├── benchmark-memory-backend-swap.py
│   ├── benchmark-memory-hierarchy.py
│   ├── benchmark-model-swap.sh
│   ├── benchmark-observability-live.sh
│   ├── benchmark-parallel.sh
│   ├── benchmark-prefill-scale.sh
│   ├── benchmark-profile-switch.sh
│   ├── benchmark-read-only-analysis-regression.py
│   ├── benchmark-recovery-e2e.py
│   ├── benchmark-repeated-read-avoidance.py
│   ├── benchmark-retriever-swap.py
│   ├── benchmark-route-backend-regression.py
│   ├── benchmark-router-live.py
│   ├── benchmark-runtime-score.py
│   ├── benchmark-runtime.py
│   ├── benchmark-source-registry-regression.py
│   ├── benchmark-tensor-split.sh
│   ├── benchmark-thinking-runtime.py
│   ├── benchmark-vector-retrieval-e2e.py
│   ├── benchmark.sh
│   ├── capture-cursor-requests.sh
│   ├── check-architecture-boundary.py
│   ├── clean-foreign-artifacts.py
│   ├── download-coder-model.sh
│   ├── download-model.sh
│   ├── download-qwen36-27b.sh
│   ├── download-qwen36.sh
│   ├── export-vision-html.sh
│   ├── export-vision-pdf.sh
│   ├── generate-dependency-graph.py
│   ├── generate-file-tree.py
│   ├── generate-project-structure.py
│   ├── logs.sh
│   ├── organize-workspace.sh
│   ├── replay-capture.py
│   ├── resume-prefill-after-wsl-reboot.sh
│   ├── run-vector-e2e.sh
│   ├── serve-vision-html.sh
│   ├── show-flow.py
│   ├── start-gateway-live.sh
│   ├── start-langfuse-local.sh
│   ├── start.sh
│   ├── stop.sh
│   ├── switch-model.sh
│   ├── tail-explorer-flow.py
│   ├── test-agent-e2e.py
│   ├── test-agent-exec.py
│   ├── test-api.sh
│   ├── test-architecture-boundary.py
│   ├── test-artifact-excerpt.py
│   ├── test-chat-fast.py
│   ├── test-context-optimizer.py
│   ├── test-dram-kv-144k.sh
│   ├── test-evidence-journal-report-e2e.py
│   ├── test-evidence-judge.py
│   ├── test-exploration-gate.py
│   ├── test-explorer-flow-format.py
│   ├── test-explorer-trace-e2e.py
│   ├── test-failed-action.py
│   ├── test-final-evidence-pack.py
│   ├── test-flow-tracing.py
│   ├── test-indexing-helpers.py
│   ├── test-intent-router.py
│   ├── test-langfuse-export.py
│   ├── test-llm-planner-shadow-e2e.py
│   ├── test-long-session-incremental.py
│   ├── test-memory-store.py
│   ├── test-message-index.py
│   ├── test-observability-export.py
│   ├── test-ping-pong-gate.py
│   ├── test-planner-promotion-gate-e2e.py
│   ├── test-planner-runtime-state-e2e.py
│   ├── test-project-index-ignore-e2e.py
│   ├── test-read-guard.py
│   ├── test-resolve-agent-phase-smoke.py
│   ├── test-router.sh
│   ├── test-runtime-inspector.py
│   ├── test-vector-retrieval.py
│   ├── test-vl-pass.py
│   ├── verify-3turn-evidence.py
│   ├── verify-3turn-evidence.sh
│   ├── verify-architecture.sh
│   ├── verify-router.sh
│   ├── verify-wsl-resources.sh
│   └── wsl-reboot-and-resume-prefill.ps1
├── ui/
│   ├── src/
│   │   ├── components/
│   │   ├── run-events/
│   │   ├── App.css
│   │   ├── App.tsx
│   │   ├── index.css
│   │   ├── main.tsx
│   │   └── vite-env.d.ts
│   ├── index.html
│   ├── package-lock.json
│   ├── package.json
│   ├── README.md
│   ├── tsconfig.json
│   ├── tsconfig.tsbuildinfo
│   └── vite.config.ts
├── .dockerignore
├── .gitignore
├── docker-compose.yml
├── handoff.md
└── README.md
```

## Excluded From Runtime Index

| path | reason | files (approx) |
|------|--------|---------------:|
| `.env` | unknown | 1 |
| `.env.backup-qwen3.6-27b` | unknown | 1 |
| `.env.example` | unknown | 1 |
| `.env.example.backup-qwen3.6-27b` | unknown | 1 |
| `docker-compose.gateway-live.yml` | unknown | 1 |
| `docker-compose.langfuse.yml` | unknown | 1 |
| `.github` | unknown | 1 |

Full inventory: `docs/reports/FILE_TREE.full.md` (optional, not indexed).

*Regenerate: `python3 scripts/generate-project-structure.py`*
