# Runtime Reachability Audit

> Generated: 2026-06-22 03:17:29 UTC

## Summary

| Usage class | Count |
|-------------|------:|
| active_hot_path | 683 |
| active_guard | 45 |
| active_optional | 3 |
| active_cli_only | 0 |
| active_test_only | 0 |
| legacy_fallback | 0 |
| imported_but_dead_branch | 19 |
| dead_candidate | 3 |
| unknown_needs_review | 7 |

## Observed at runtime (sample)

| Module | Symbol | Class |
|--------|--------|-------|

## Imported but dead branch

| Module | Env | Recommendation |
|--------|-----|----------------|
| `legacy.context_optimizer` | CONTEXT_OPTIMIZER | deprecate_env |
| `legacy.context_optimizer` | CONTEXT_OPTIMIZER | deprecate_env |
| `legacy.context_optimizer` | CONTEXT_OPTIMIZER | deprecate_env |
| `legacy.context_optimizer` | CONTEXT_OPTIMIZER | deprecate_env |
| `legacy.context_optimizer` | CONTEXT_OPTIMIZER | deprecate_env |
| `legacy.context_optimizer` | CONTEXT_OPTIMIZER | deprecate_env |
| `legacy.context_optimizer` | CONTEXT_OPTIMIZER | deprecate_env |
| `legacy.runtime_optimizer` | RUNTIME_OPTIMIZER | deprecate_env |
| `legacy.runtime_optimizer` | RUNTIME_OPTIMIZER | deprecate_env |
| `legacy.runtime_optimizer` | RUNTIME_OPTIMIZER | deprecate_env |
| `legacy.runtime_optimizer` | RUNTIME_OPTIMIZER | deprecate_env |
| `legacy.runtime_optimizer` | RUNTIME_OPTIMIZER | deprecate_env |
| `legacy.runtime_optimizer` | RUNTIME_OPTIMIZER | deprecate_env |
| `legacy.runtime_optimizer` | RUNTIME_OPTIMIZER | deprecate_env |
| `legacy.runtime_optimizer` | RUNTIME_OPTIMIZER | deprecate_env |
| `legacy.runtime_optimizer` | RUNTIME_OPTIMIZER | deprecate_env |
| `legacy.runtime_optimizer` | RUNTIME_OPTIMIZER | deprecate_env |
| `legacy.runtime_optimizer` | RUNTIME_OPTIMIZER | deprecate_env |
| `legacy.runtime_optimizer` | RUNTIME_OPTIMIZER | deprecate_env |

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
