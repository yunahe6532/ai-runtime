# Deprecated Environment Variables

> Generated: 2026-06-22 03:21:36 UTC

## Removed from compose (archived 2026-06-22)

| Env | Former default | Status | Replacement |
|-----|----------------|--------|-------------|
| `CONTEXT_OPTIMIZER` | 1 | **archived** | `runtime_core/indexing_helpers` + `dynamic_context_scheduler` |
| `RUNTIME_OPTIMIZER` | 1 | **archived** | `prompt_enforcer` + `dynamic_context_scheduler` |

Stub modules remain at `router/legacy/context_optimizer.py` and `runtime_optimizer.py`.
Full sources: `~/.local/share/ai-runtime/archive/deprecated/20260622/legacy/`.

## Active optional (default off)

| Env | Default | Module |
|-----|---------|--------|
| `LLM_PLANNER_SHADOW_ENABLED` | 0 | `agent_brain/llm_planner.py` |
| `LLAMAINDEX_ENABLED` | 0 | `integrations/llamaindex.py` |

Remove from `.env.example` active section only after archive phase completes.

## Planned (v2 — not archive)

| Env | Default | Module |
|-----|---------|--------|
| `MCP_ENABLED` | 0 | `adapters/mcp.py` (stub) |

