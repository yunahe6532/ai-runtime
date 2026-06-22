# Repo Inventory Audit

> Generated: 2026-06-22 03:21:36 UTC
> Root: `/home/yunahe/ai-runtime/cursor-local-llm`

## Summary

| Metric | Value |
|--------|------:|
| Files | 1,176 |
| Dirs (walked) | 59 |
| Total size | 6.9 MB |

## Classification

| Class | Files | Size (MB) |
|-------|------:|----------:|
| runtime_logs | 773 | 1.8 |
| source | 112 | 1.3 |
| scripts | 108 | 0.8 |
| cache | 103 | 1.7 |
| docs | 34 | 1.3 |
| git_metadata | 28 | 0.1 |
| config | 10 | 0.0 |
| unknown | 7 | 0.0 |
| benchmark_outputs | 1 | 0.0 |

## Top-level directories

| Dir | Files | Size (MB) |
|-----|------:|----------:|
| `router` | 950 | 3.1 |
| `scripts` | 115 | 0.9 |
| `docs` | 31 | 1.2 |
| `.git` | 28 | 0.1 |
| `ui` | 23 | 0.3 |
| `(root)` | 11 | 0.1 |
| `tmp` | 11 | 1.2 |
| `configs` | 5 | 0.0 |
| `.github` | 1 | 0.0 |
| `config` | 1 | 0.0 |

## Git state

- **ignored**: 881
- **tracked**: 265
- **unknown**: 28
- **untracked**: 2

## Mtime distribution

- 7d: 1,176

## Large files (≥500KB)

| Path | MB | Class |
|------|---:|-------|
| `docs/VISION.pdf` | 0.9 | docs |
| `tmp/runtime-reachability-static.json` | 0.6 | runtime_logs |

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

- `tmp/profile-test-ping-pong-gate.json (runtime_logs)`
- `tmp/foreign-artifacts-audit.json (runtime_logs)`
- `tmp/runtime-reachability-profile.json (runtime_logs)`
- `tmp/profile-benchmark-recovery-e2e.json (benchmark_outputs)`
- `tmp/runtime-reachability.json (runtime_logs)`
- `tmp/repo-inventory.json (runtime_logs)`
- `tmp/legacy-archive-applied.json (runtime_logs)`
- `tmp/runtime-reachability-static.json (runtime_logs)`
- `tmp/_reachability_profiler.py (runtime_logs)`
- `tmp/.gitkeep (runtime_logs)`
- `tmp/dead-code-audit.json (runtime_logs)`
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

## Runtime storage

Runtime artifacts should live under `AI_RUNTIME_DATA_DIR` (default `~/.local/share/ai-runtime`).
Repo `tmp/` is fallback only.

*Regenerate: `python3 scripts/audit-repo-inventory.py`*
