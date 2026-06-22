# Deprecated Branches (env-gated, not observed)

> Generated: 2026-06-22 03:17:29 UTC

| Env | Default | Modules | Reachable | Dead branch |
|-----|---------|---------|-----------|-------------|
| `MEMORY_STATE_BODY` | 1 | 2 | 2 | no |
| `DYNAMIC_BUDGET` | 1 | 3 | 3 | no |
| `COVERAGE_CHECK` | 1 | 2 | 2 | no |
| `RECOVERY_ENABLED` | 1 | 2 | 2 | no |
| `VECTOR_RETRIEVAL` | 1 | 2 | 2 | no |
| `LLAMAINDEX_ENABLED` | 0 | 2 | 2 | no |
| `EVIDENCE_JUDGE_ENABLED` | 1 | 1 | 1 | no |
| `LLM_PLANNER_SHADOW_ENABLED` | 0 | 1 | 1 | no |
| `PLANNER_PROMOTION_GATE_ENABLED` | 1 | 1 | 1 | no |
| `PLANNER_PROMOTION_SHADOW_ONLY` | 1 | 1 | 1 | no |
| `CONTEXT_OPTIMIZER` | 1 | 1 | 0 | yes |
| `RUNTIME_OPTIMIZER` | 1 | 1 | 0 | yes |

## CONTEXT_OPTIMIZER / RUNTIME_OPTIMIZER

- `legacy/context_optimizer.py` — **no import path** from entrypoints; env references only in dead module
- `legacy/runtime_optimizer.py` — same; docker-compose still sets `=1` but code never imports
- **Recommendation:** move env to Deprecated section; do not archive until reachability merge confirms

