# Legacy Archive Plan

> Generated: 2026-06-22 03:17:29 UTC

| File | Referenced | Last modified (mtime) | Related env | Risk | Action |
|------|------------|---------------------|-------------|------|--------|
| `router/legacy/__init__.py` | yes | 2026-06-21 | LEGACY_OPTIMIZER | high | keep |
| `router/legacy/agent_runs.py` | yes | 2026-06-21 | AGENT_RUNS_DIR, AGENT_RUNS_ENABLED, AGENT_RUNS_MAX_EVENTS | high | keep |
| `router/legacy/context_optimizer.py` | no | 2026-06-21 | CONTEXT_OPTIMIZER, OPTIMIZER_ERROR_KEEP_CHARS, OPTIMIZER_MODE | medium | deprecate_env |
| `router/legacy/memory_store.py` | yes | 2026-06-22 | CONTEXT_CACHE_DIR, MEMORY_STORE | high | keep |
| `router/legacy/retriever.py` | yes | 2026-06-21 | - | medium | deprecate_env |
| `router/legacy/runtime_optimizer.py` | no | 2026-06-21 | RUNTIME_OPTIMIZER, RUNTIME_OPTIMIZER_MIN_TOOL_CHARS, RUNTIME_OPTIMIZER_SHRINK_STEP | medium | deprecate_env |
| duplicate extract_recent_agent_tail | ? | - | - | medium | manual_review |
| duplicate extract_original_system | ? | - | - | medium | manual_review |
| old build_context_pack path | ? | - | - | medium | deprecate_env |

## Action glossary

- **keep** — active or reachable fallback
- **move_to_archive** — unreachable, no importers
- **deprecate_env** — gated by env, document before removal
- **merge** — duplicate logic consolidation
- **delete_after_tests** — remove only after coverage proof
- **manual_review** — human decision required
