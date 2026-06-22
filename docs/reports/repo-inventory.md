# Repo Inventory Audit

> Generated: 2026-06-22 03:17:28 UTC
> Root: `/home/yunahe/ai-runtime/cursor-local-llm`

## Summary

| Metric | Value |
|--------|------:|
| Files | 1,165 |
| Dirs (walked) | 59 |
| Total size | 5.7 MB |

## Classification

| Class | Files | Size (MB) |
|-------|------:|----------:|
| runtime_logs | 765 | 0.6 |
| source | 112 | 1.3 |
| scripts | 107 | 0.8 |
| cache | 103 | 1.7 |
| docs | 33 | 1.3 |
| git_metadata | 28 | 0.1 |
| config | 10 | 0.0 |
| unknown | 7 | 0.0 |

## Top-level directories

| Dir | Files | Size (MB) |
|-----|------:|----------:|
| `router` | 950 | 3.2 |
| `scripts` | 114 | 0.9 |
| `docs` | 30 | 1.2 |
| `.git` | 28 | 0.1 |
| `ui` | 23 | 0.3 |
| `(root)` | 11 | 0.1 |
| `configs` | 5 | 0.0 |
| `tmp` | 2 | 0.0 |
| `.github` | 1 | 0.0 |
| `config` | 1 | 0.0 |

## Git state

- **ignored**: 872
- **tracked**: 261
- **unknown**: 28
- **untracked**: 4

## Mtime distribution

- 7d: 1,165

## Large files (≥500KB)

| Path | MB | Class |
|------|---:|-------|
| `docs/VISION.pdf` | 0.9 | docs |

## Project Index policy

### Include samples

- `.gitignore`
- `README.md`
- `.dockerignore`
- `docker-compose.yml`
- `handoff.md`
- `config/runtime_self_model.yaml`
- `scripts/clean-foreign-artifacts.py`
- `scripts/stop.sh`
- `scripts/test-observability-export.py`
- `scripts/audit-runtime-reachability.py`
- `scripts/verify-architecture.sh`
- `scripts/capture-cursor-requests.sh`
- `scripts/test-failed-action.py`
- `scripts/test-runtime-inspector.py`
- `scripts/benchmark-observability-live.sh`
- `scripts/benchmark-vector-retrieval-e2e.py`
- `scripts/switch-model.sh`
- `scripts/analyze-conversation-flow.py`
- `scripts/benchmark-cursor-agent.py`
- `scripts/resume-prefill-after-wsl-reboot.sh`

### Exclude samples

- `tmp/foreign-artifacts-audit.json (runtime_logs)`
- `tmp/.gitkeep (runtime_logs)`
- `scripts/__pycache__/benchmark-read-only-analysis-regression.cpython-312.pyc (cache)`
- `scripts/__pycache__/benchmark-memory-hierarchy.cpython-312.pyc (cache)`
- `scripts/__pycache__/benchmark-gateway-swap.cpython-312.pyc (cache)`
- `scripts/__pycache__/benchmark-recovery-e2e.cpython-312.pyc (cache)`
- `scripts/__pycache__/show-flow.cpython-312.pyc (cache)`
- `scripts/__pycache__/benchmark-cursor-agent.cpython-312.pyc (cache)`
- `scripts/__pycache__/check-architecture-boundary.cpython-312.pyc (cache)`
- `router/agent_brain/__pycache__/planner_contract.cpython-312.pyc (cache)`
- `router/agent_brain/__pycache__/promotion_gate.cpython-312.pyc (cache)`
- `router/agent_brain/__pycache__/__init__.cpython-312.pyc (cache)`
- `router/agent_brain/__pycache__/planner_shadow.cpython-312.pyc (cache)`
- `router/agent_brain/__pycache__/runtime_state.cpython-312.pyc (cache)`
- `router/agent_brain/__pycache__/llm_planner.cpython-312.pyc (cache)`
- `router/adapters/__pycache__/gateway.cpython-312.pyc (cache)`
- `router/adapters/__pycache__/retrieval.cpython-312.pyc (cache)`
- `router/adapters/__pycache__/trace.cpython-312.pyc (cache)`
- `router/adapters/__pycache__/__init__.cpython-312.pyc (cache)`
- `router/adapters/__pycache__/memory.cpython-312.pyc (cache)`
- `router/adapters/__pycache__/langgraph.cpython-312.pyc (cache)`
- `router/adapters/__pycache__/observe.cpython-312.pyc (cache)`
- `router/tmp/context-cache/current_state.json (runtime_logs)`
- `router/tmp/context-cache/projects/_registry.json (runtime_logs)`
- `router/tmp/context-cache/projects/testproj/artifacts/fa_test.json (runtime_logs)`

## Runtime storage

Runtime artifacts should live under `AI_RUNTIME_DATA_DIR` (default `~/.local/share/ai-runtime`).
Repo `tmp/` is fallback only.

*Regenerate: `python3 scripts/audit-repo-inventory.py`*
