# Benchmark Known Flaky Tasks

> Updated: 2026-06-22

## `shell_logs` category (t00N_shell_logs)

**Not a promotion regression signal.** Treat separately from Phase 2.2b apply tests.

| Symptom | Typical range |
|---------|----------------|
| `runtime_tool_calls` > `max_runtime_tools` (1) | 3–4 Shell emissions |
| Pass rate | **28–30 / 30** depending on LLM/tool loop variance |

### Root cause (observed)

- Task expects a single `Shell` for `docker logs … | tail`.
- Router occasionally emits multiple `Shell` tool calls before satisfying the scenario.
- Unrelated to `PLANNER_PROMOTION_*` env (same flakiness with default and promotion-on runs).

### Verification guidance

```bash
# Default hot path (promotion off)
python3 scripts/benchmark-runtime-score.py --tasks 30

# Promotion live (read/grep/glob only)
PLANNER_PROMOTION_SHADOW_ONLY=0 PLANNER_PROMOTION_ENABLE_READONLY=1 \
  python3 scripts/benchmark-runtime-score.py --tasks 30
```

If only `*_shell_logs` fails, re-run once after `docker compose up -d router`.  
Do **not** block promotion merge on a single shell_logs miss when all other tasks pass.
