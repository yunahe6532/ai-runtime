#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"
PORT="${PORT:-8080}"
BASE="http://localhost:${PORT}"

echo "=== Router status ==="
curl -fsS "${BASE}/router/status" | python3 -m json.tool

echo ""
echo "=== Small request (expect fast) ==="
curl -fsS "${BASE}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy" \
  -d '{"model":"model.gguf","messages":[{"role":"user","content":"ping"}],"max_tokens":8,"stream":false}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("ok", d["usage"])'

echo ""
curl -fsS "${BASE}/router/status" | python3 -c 'import json,sys; print("active:", json.load(sys.stdin)["active_backend"])'

echo ""
echo "=== Done (large prompt routing: use prefill prompt file manually) ==="
