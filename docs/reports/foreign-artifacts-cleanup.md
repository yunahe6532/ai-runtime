# Foreign Artifacts Cleanup

> Generated: 2026-06-22 03:16:55 UTC
> Mode: **apply**
> Data dir: `/home/yunahe/.local/share/ai-runtime`

| Source | Destination | Action | Files | MB | Status |
|--------|-------------|--------|------:|---:|--------|
| `tmp/cursor-captures` | `/home/yunahe/.local/share/ai-runtime/captures` | move_runtime_merge | 2,656 | 19.3 | error |
| `tmp/context-cache` | `/home/yunahe/.local/share/ai-runtime/cache/context-cache` | move_runtime_merge | 9,986 | 456.0 | error |
| `tmp/foreign-artifacts-cleanup.json` | `/home/yunahe/.local/share/ai-runtime/benchmarks/foreign-artifacts-cleanup.json` | move_runtime_file | 1 | 0.0 | moved |

## Errors

- `tmp/cursor-captures`: error [Errno 13] Permission denied: '/home/yunahe/ai-runtime/cursor-local-llm/tmp/cursor-captures/agent-runs/1782097052_0039.json'
- `tmp/context-cache`: error [Errno 13] Permission denied: '/home/yunahe/ai-runtime/cursor-local-llm/tmp/context-cache/raw/1782097052_0039.json'

*Re-run audit after cleanup: `python3 scripts/audit-foreign-artifacts.py`*
