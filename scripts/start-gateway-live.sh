#!/usr/bin/env bash
# Start llama-long (+ optional LiteLLM) with host port exposure for GATEWAY_LIVE benches.
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.gateway-live.yml)
START_LITELLM="${START_LITELLM:-1}"
WAIT_SEC="${GATEWAY_LIVE_WAIT_SEC:-300}"

if [[ ! -f ".env" ]]; then
  echo "ERROR: .env missing — copy from .env.example"
  exit 1
fi

# shellcheck disable=SC1091
source ".env"

CODER_MODEL_FILE="${CODER_MODEL_FILE/#\~/$HOME}"
if [[ ! -f "${CODER_MODEL_FILE}" ]]; then
  echo "ERROR: CODER model missing: ${CODER_MODEL_FILE}"
  echo "  Run: ./scripts/download-qwen36.sh  or  ./scripts/switch-model.sh <profile>"
  exit 1
fi

export CODER_MODEL_FILE

wait_http() {
  local url="$1"
  local label="$2"
  local deadline=$((SECONDS + WAIT_SEC))
  echo "Waiting for ${label} (${url}) up to ${WAIT_SEC}s ..."
  while (( SECONDS < deadline )); do
    if curl -fsS --max-time 3 "${url}" >/dev/null 2>&1; then
      echo "  ${label} ready"
      return 0
    fi
    sleep 3
  done
  echo "ERROR: ${label} not ready at ${url} after ${WAIT_SEC}s"
  echo "  Check: docker compose -f docker-compose.yml -f docker-compose.gateway-live.yml logs llama-long"
  return 1
}

echo "=== gateway live profile ==="
echo "Starting llama-long (host :8082) ..."
"${COMPOSE[@]}" up -d llama-long

wait_http "http://127.0.0.1:8082/v1/models" "llama-long"

if [[ "${START_LITELLM}" == "1" ]]; then
  echo "Starting LiteLLM proxy (host :4000 → llama-long) ..."
  "${COMPOSE[@]}" up -d litellm
  wait_http "http://127.0.0.1:4000/health" "litellm"
fi

cat <<EOF

Gateway live endpoints:
  LONG_URL=http://127.0.0.1:8082
  FAST_URL=http://127.0.0.1:8081
  LITELLM_URL=http://127.0.0.1:4000

Run benches:
  GATEWAY_LIVE=1 LONG_URL=http://127.0.0.1:8082 BACKEND=llama_cpp python3 scripts/benchmark-gateway-swap.py
  GATEWAY_LIVE=1 LITELLM_URL=http://127.0.0.1:4000 BACKEND=litellm python3 scripts/benchmark-gateway-swap.py
  ./scripts/benchmark-gateway-live.sh
EOF
