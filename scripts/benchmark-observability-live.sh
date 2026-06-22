#!/usr/bin/env bash
# Observability live: Langfuse OTel export + optional gateway live bench.
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

export LANGFUSE_LIVE=1
export LANGFUSE_OTEL=1
export OTEL_FLOW_TRACE=1
export OTEL_EVENT_CAPTURE=1

if [[ -f "configs/langfuse-local.env" ]] && [[ -z "${LANGFUSE_PUBLIC_KEY:-}" ]]; then
  # shellcheck disable=SC1091
  source "configs/langfuse-local.env"
fi

START_LANGFUSE="${START_LANGFUSE:-1}"
RUN_GATEWAY_LIVE="${RUN_GATEWAY_LIVE:-0}"

if [[ "${START_LANGFUSE}" == "1" ]] && [[ -z "${LANGFUSE_PUBLIC_KEY:-}" ]]; then
  echo "WARN: LANGFUSE_PUBLIC_KEY unset — trying ./scripts/start-langfuse-local.sh"
  bash scripts/start-langfuse-local.sh
  # shellcheck disable=SC1091
  source "configs/langfuse-local.env"
fi

echo "=== [1/3] Architecture boundary ==="
python3 scripts/check-architecture-boundary.py

echo ""
echo "=== [2/3] Langfuse OTel export (LANGFUSE_LIVE=1) ==="
python3 scripts/test-langfuse-export.py

if [[ "${RUN_GATEWAY_LIVE}" == "1" ]]; then
  echo ""
  echo "=== [3/3] Gateway live (mock backend, Langfuse trace already verified) ==="
  GATEWAY_LIVE=1 BACKEND=mock python3 scripts/benchmark-gateway-swap.py
else
  echo ""
  echo "=== [3/3] Gateway live bench skipped (RUN_GATEWAY_LIVE=1 to enable) ==="
fi

echo ""
echo "=== observability live complete ==="
