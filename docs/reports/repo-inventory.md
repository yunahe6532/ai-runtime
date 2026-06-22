# Repo Inventory Audit

> Generated: 2026-06-22 02:56:18 UTC
> Root: `/home/yunahe/ai-runtime/cursor-local-llm`

## Summary

| Metric | Value |
|--------|------:|
| Files | 48,765 |
| Dirs (walked) | 2,348 |
| Total size | 1042.5 MB |

## Classification

| Class | Files | Size (MB) |
|-------|------:|----------:|
| node_modules | 35,929 | 499.6 |
| runtime_logs | 12,344 | 475.6 |
| source | 112 | 1.3 |
| scripts | 104 | 0.7 |
| cache | 103 | 1.7 |
| captures | 86 | 58.1 |
| unknown | 37 | 0.1 |
| docs | 24 | 1.2 |
| benchmark_outputs | 17 | 0.3 |
| config | 8 | 0.0 |
| generated_docs | 1 | 3.8 |

## Top-level directories

| Dir | Files | Size (MB) |
|-----|------:|----------:|
| `scripts` | 33,094 | 415.5 |
| `tmp` | 11,684 | 533.5 |
| `ui` | 2,969 | 85.2 |
| `router` | 950 | 3.2 |
| `.git` | 28 | 0.1 |
| `docs` | 22 | 5.0 |
| `(root)` | 11 | 0.1 |
| `configs` | 5 | 0.0 |
| `.github` | 1 | 0.0 |
| `config` | 1 | 0.0 |

## Git state

- **ignored**: 48,483
- **tracked**: 242
- **unknown**: 28
- **untracked**: 12

## Mtime distribution

- 7d: 48,765

## Large files (≥500KB)

| Path | MB | Class |
|------|---:|-------|
| `tmp/download-qwen36.log` | 34.0 | captures |
| `scripts/pdf-export/node_modules/@napi-rs/canvas-linux-x64-gnu/skia.linux-x64-gnu.node` | 31.8 | node_modules |
| `scripts/pdf-export/node_modules/@napi-rs/canvas-linux-x64-musl/skia.linux-x64-musl.node` | 28.4 | node_modules |
| `tmp/download-qwen3627.log` | 23.6 | captures |
| `scripts/pdf-export/node_modules/mermaid/dist/mermaid.js.map` | 11.7 | node_modules |
| `scripts/pdf-export/node_modules/mermaid/dist/mermaid.min.js.map` | 11.6 | node_modules |
| `ui/node_modules/.bin/esbuild` | 9.9 | node_modules |
| `ui/node_modules/esbuild/bin/esbuild` | 9.9 | node_modules |
| `ui/node_modules/@esbuild/linux-x64/bin/esbuild` | 9.9 | node_modules |
| `ui/node_modules/typescript/lib/typescript.js` | 8.7 | node_modules |
| `scripts/pdf-export/node_modules/mermaid/dist/mermaid.js` | 7.3 | node_modules |
| `ui/node_modules/typescript/lib/_tsc.js` | 5.9 | node_modules |
| `scripts/pdf-export/node_modules/@zenuml/core/dist/zenuml.js.map` | 5.7 | node_modules |
| `scripts/pdf-export/node_modules/@mermaid-js/mermaid-zenuml/dist/mermaid-zenuml.js.map` | 5.2 | node_modules |
| `scripts/pdf-export/node_modules/@fortawesome/fontawesome-free/metadata/icon-families.json` | 5.1 | node_modules |
| `scripts/pdf-export/node_modules/@mermaid-js/mermaid-zenuml/dist/mermaid-zenuml.min.js.map` | 5.0 | node_modules |
| `scripts/pdf-export/node_modules/@mermaid-js/layout-elk/dist/chunks/mermaid-layout-elk.esm.min/render-T6MDALS3.mjs.map` | 4.9 | node_modules |
| `scripts/pdf-export/node_modules/elkjs/lib/elk-worker.js` | 4.6 | node_modules |
| `scripts/pdf-export/node_modules/@mermaid-js/layout-elk/dist/chunks/mermaid-layout-elk.esm/render-BHGI7IPK.mjs.map` | 4.5 | node_modules |
| `scripts/pdf-export/node_modules/cytoscape-fcose/demo/demo.gif` | 4.4 | node_modules |
| `scripts/pdf-export/node_modules/chromium-bidi/lib/iife/mapperTab.js.map` | 4.2 | node_modules |
| `scripts/pdf-export/node_modules/@zenuml/core/dist/zenuml.esm.mjs.map` | 4.2 | node_modules |
| `scripts/pdf-export/node_modules/@mermaid-js/mermaid-zenuml/dist/mermaid-zenuml.js` | 4.2 | node_modules |
| `tmp/context-cache/raw/1782048089_0003.json` | 4.1 | runtime_logs |
| `tmp/context-cache/raw/1782048078_0002.json` | 4.1 | runtime_logs |

## Project Index policy

### Include samples

- `README.md`
- `docker-compose.yml`
- `handoff.md`
- `config/runtime_self_model.yaml`
- `scripts/stop.sh`
- `scripts/test-observability-export.py`
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
- `scripts/start-langfuse-local.sh`
- `scripts/test-evidence-judge.py`
- `scripts/run-vector-e2e.sh`
- `scripts/verify-router.sh`

### Exclude samples

- `tmp/benchmark-memory-hierarchy.json (benchmark_outputs)`
- `tmp/langfuse-export-result.json (runtime_logs)`
- `tmp/conversation-flow-report.json (runtime_logs)`
- `tmp/benchmark-router-live.json (benchmark_outputs)`
- `tmp/benchmark-cursor-agent-qwen3627b.log (benchmark_outputs)`
- `tmp/benchmark-runtime-score-qwen3627b.log (benchmark_outputs)`
- `tmp/benchmark-score-100.log (benchmark_outputs)`
- `tmp/benchmark-runtime-qwen3627b.log (benchmark_outputs)`
- `tmp/benchmark-runtime-score-p1.log (benchmark_outputs)`
- `tmp/download-qwen36.log (captures)`
- `tmp/benchmark-runtime.json (benchmark_outputs)`
- `tmp/benchmark-runtime-score.json (benchmark_outputs)`
- `tmp/benchmark-cursor-agent.json (benchmark_outputs)`
- `tmp/download-qwen3627.log (captures)`
- `tmp/agent-e2e-results.json (runtime_logs)`
- `tmp/benchmark-retriever-swap.json (benchmark_outputs)`
- `tmp/benchmark-gateway-swap.json (benchmark_outputs)`
- `tmp/benchmark-cursor-agent-p1.log (benchmark_outputs)`
- `tmp/benchmark-thinking-qwen3627b.log (benchmark_outputs)`
- `tmp/dependency-graph.json (runtime_logs)`
- `tmp/.gitkeep (runtime_logs)`
- `tmp/benchmark-memory-backend-swap.json (benchmark_outputs)`
- `tmp/benchmark-coder-vs-vl.json (benchmark_outputs)`
- `tmp/compose.resolved.yml (runtime_logs)`
- `tmp/tensor-split-bench/ctx4096_split5050.smi.csv (runtime_logs)`

## Runtime storage

Runtime artifacts should live under `AI_RUNTIME_DATA_DIR` (default `~/.local/share/ai-runtime`).
Repo `tmp/` is fallback only.

*Regenerate: `python3 scripts/audit-repo-inventory.py`*
