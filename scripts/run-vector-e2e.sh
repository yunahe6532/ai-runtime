#!/usr/bin/env bash
# Vector retrieval E2E — BM25 (system python) + LlamaIndex (optional venv)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export CONTEXT_CACHE_DIR="$ROOT/tmp/context-cache"
export VECTOR_RETRIEVAL=1
export VECTOR_E2E_PROJECT="${VECTOR_E2E_PROJECT:-e5903e2a81c2}"

echo "=== Phase 1: builtin BM25 (LLAMAINDEX_ENABLED=0) ==="
export LLAMAINDEX_ENABLED=0
python3 scripts/benchmark-vector-retrieval-e2e.py

VENV="$ROOT/.venv-llamaindex"
if [[ -x "$VENV/bin/python" ]]; then
  PY="$VENV/bin/python"
else
  echo "Creating .venv-llamaindex ..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q llama-index-core httpx
  PY="$VENV/bin/python"
fi

echo ""
echo "=== Phase 2: LlamaIndex (LLAMAINDEX_ENABLED=1) ==="
export LLAMAINDEX_ENABLED=1
"$PY" scripts/benchmark-vector-retrieval-e2e.py

echo ""
echo "vector retrieval E2E complete (BM25 + LlamaIndex)"
