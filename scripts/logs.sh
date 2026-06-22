#!/usr/bin/env bash
set -euo pipefail

cd "/home/yunahe/ai-runtime/cursor-local-llm"

if [[ -f ".env" ]]; then
  # shellcheck disable=SC1091
  source ".env"
  MODEL_FILE="${MODEL_FILE:-}"
  MODEL_FILE="${MODEL_FILE/#\~/$HOME}"
  export MODEL_FILE
fi

docker compose logs -f
