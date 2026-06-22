# Deprecated Environment Variables

> Generated: 2026-06-22 03:11:19 UTC

Env vars that reference code with **no runtime reachability** from entrypoints.

| Env | Default (compose) | Status | Module |
|-----|-------------------|--------|--------|
| `CONTEXT_OPTIMIZER` | 1 | **deprecated** — no import path | `legacy/context_optimizer.py` |
| `RUNTIME_OPTIMIZER` | 1 | **deprecated** — no import path | `legacy/runtime_optimizer.py` |

## Active optional (default off)

| Env | Default | Module |
|-----|---------|--------|
| `LLM_PLANNER_SHADOW_ENABLED` | 0 | `agent_brain/llm_planner.py` |
| `LLAMAINDEX_ENABLED` | 0 | `integrations/llamaindex.py` |

Remove from `.env.example` active section only after archive phase completes.

