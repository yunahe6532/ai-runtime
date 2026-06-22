#!/usr/bin/env bash
# Full gateway live A/B: start infra → llama_cpp → litellm → mock → boundary.
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

export OTEL_EVENT_CAPTURE=1
export OTEL_FLOW_TRACE=1
export LONG_URL="${LONG_URL:-http://127.0.0.1:8082}"
export FAST_URL="${FAST_URL:-http://127.0.0.1:8081}"
export LITELLM_URL="${LITELLM_URL:-http://127.0.0.1:4000}"
export GATEWAY_LIVE=1

echo "=== [0/4] Start gateway live profile ==="
bash scripts/start-gateway-live.sh

echo ""
echo "=== [1/4] Architecture boundary ==="
python3 scripts/check-architecture-boundary.py

echo ""
echo "=== [2/4] llama_cpp live ==="
GATEWAY_LIVE=1 BACKEND=llama_cpp LONG_URL="${LONG_URL}" python3 scripts/benchmark-gateway-swap.py

echo ""
echo "=== [3/4] litellm live ==="
GATEWAY_LIVE=1 BACKEND=litellm LITELLM_URL="${LITELLM_URL}" python3 scripts/benchmark-gateway-swap.py

echo ""
echo "=== [4/4] mock offline (CI baseline) ==="
GATEWAY_LIVE=0 BACKEND=mock python3 scripts/benchmark-gateway-swap.py

echo ""
echo "=== gateway live A/B complete ==="
cat tmp/benchmark-gateway-swap.json | python3 -m json.tool | head -40
