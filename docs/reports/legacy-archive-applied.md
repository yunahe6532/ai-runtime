# Legacy Archive Applied

> Phase 2.2a-clean3 — 2026-06-22

## Optimizer modules (archived)

| Source | Archive destination | Judgment |
|--------|---------------------|----------|
| `router/legacy/context_optimizer.py` | `~/.local/share/ai-runtime/archive/deprecated/20260622/legacy/context_optimizer.py` | import 0 from entrypoints; env removed from compose; logic superseded by `runtime_core/indexing_helpers` + `dynamic_context_scheduler` |
| `router/legacy/runtime_optimizer.py` | `~/.local/share/ai-runtime/archive/deprecated/20260622/legacy/runtime_optimizer.py` | import 0; `RUNTIME_OPTIMIZER` removed from compose; superseded by `prompt_enforcer` + dynamic scheduler |

Repo stubs remain at original paths (raise `ImportError` on attribute access).  
Optional offline test: `LEGACY_OPTIMIZER=1 python3 scripts/test-context-optimizer.py` loads archive copy.

Script: `python3 scripts/archive-deprecated-legacy.py --apply`

## Deprecated env removed from compose

| Env | Action |
|-----|--------|
| `CONTEXT_OPTIMIZER` | Removed from `docker-compose.yml`; moved to Deprecated section in `.env.example` |
| `RUNTIME_OPTIMIZER` | Removed from `docker-compose.yml`; moved to Deprecated section in `.env.example` |
| `OPTIMIZER_MODE`, `OPTIMIZER_RECENT_*` | Removed from compose; commented in `.env.example` |

## D-tier manual review (runtime-reachability merge)

Three `dead_candidate` symbols — all in one file:

| file | imported | reachable_static | observed_runtime | env_gate | test_usage | cli_usage | recommendation |
|------|----------|------------------|------------------|----------|------------|-----------|----------------|
| `router/adapters/mcp.py` (`<module>`) | no | no | no | — | no | no | **unknown_needs_review** — v2 planned stub; keep |
| `router/adapters/mcp.py` (`mcp_enabled`) | no | no | no | `MCP_ENABLED` (default 0, not in TRACKED_ENVS) | no | no | **unknown_needs_review** — future MCP integration; do not archive |
| `router/adapters/mcp.py` (`normalize_tool_result`) | no | no | no | — | no | no | **unknown_needs_review** — placeholder API; do not archive |

**Decision:** No archive move for `adapters/mcp.py`. Documented as planned v2 scope (`MCP_ENABLED=0`).

## Not touched (per plan)

- `legacy/memory_store.py`, `legacy/retriever.py`, `legacy/agent_runs.py`
- `build_context_pack` legacy path, `MEMORY_STATE_BODY=0` fallback
- `reference/` hot path files
