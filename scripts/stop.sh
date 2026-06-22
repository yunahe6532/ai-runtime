#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

if [[ -f ".env" ]]; then
  # shellcheck disable=SC1091
  source ".env"
  MODEL_FILE="${MODEL_FILE:-}"
  MODEL_FILE="${MODEL_FILE/#\~/$HOME}"
  export MODEL_FILE
fi

docker compose down
docker rm -f cursor-local-llm cursor-local-llm-fast cursor-local-llm-long cursor-local-llm-router 2>/dev/null || true
