#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
CAPTURE_DIR="${RUNTIME_DIR}/tmp/cursor-captures"
cd "${RUNTIME_DIR}"

mkdir -p "${CAPTURE_DIR}"

echo "=== Cursor request capture ON ==="
echo "capture dir: ${CAPTURE_DIR}"
echo
echo "1) Cursor에서 채팅 3~5번 보내기"
echo "2) 분석: ./scripts/analyze-cursor-captures.py"
echo

export CAPTURE_REQUESTS=1
export CAPTURE_DIR=/captures

docker compose up -d --build router
docker compose exec -T router sh -lc 'ls -la /captures 2>/dev/null || true'

echo
echo "router capture enabled. use Cursor, then run:"
echo "  ./scripts/analyze-cursor-captures.py"
