# Foreign Artifacts Audit

> Generated: 2026-06-22 03:17:28 UTC
> Root: `/home/yunahe/ai-runtime/cursor-local-llm`

## Summary

| Class | Paths | Files | Size (MB) |
|-------|------:|------:|----------:|
| project_source | 8 | 961 | 3.2 |
| runtime_artifact | 1 | 1 | 0.0 |
| foreign_project | 1 | 5 | 0.1 |
| unknown_review | 5 | 320 | 159.5 |

## Priority targets

| Path | Class | Files | MB | Action | Linked |
|------|-------|------:|---:|--------|--------|
| `scripts/pdf-export` | foreign_project | 5 | 0.1 | review | True |
| `tmp` | runtime_artifact | 1 | 0.0 | move_runtime | True |

## Cleanup recommendations

- `scripts/pdf-export/node_modules` → **move** to `archive/vendor-dumps/` (keep `.mjs` + `package.json`)
- `.venv-llamaindex` → **move** to `archive/vendor-dumps/`
- `tmp/*` → **move** to `AI_RUNTIME_DATA_DIR` (repo keeps `tmp/.gitkeep` only)
- `docs/reports/FILE_TREE.full.md` → **move** to `archive/file-tree/`

*Regenerate: `python3 scripts/audit-foreign-artifacts.py`*
