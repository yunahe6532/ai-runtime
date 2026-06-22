#!/usr/bin/env bash
# Start self-hosted Langfuse for LANGFUSE_LIVE=1 (host :3100).
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

WAIT_SEC="${LANGFUSE_WAIT_SEC:-180}"

echo "=== Langfuse local profile (:3100) ==="
docker compose -f docker-compose.langfuse.yml up -d

deadline=$((SECONDS + WAIT_SEC))
echo "Waiting for Langfuse health (up to ${WAIT_SEC}s) ..."
while (( SECONDS < deadline )); do
  if curl -fsS --max-time 3 http://127.0.0.1:3100/api/public/health >/dev/null 2>&1; then
    echo "Langfuse ready: http://localhost:3100"
    echo "Keys: configs/langfuse-local.env"
    echo "  email: admin@example.com  password: adminadmin123"
    exit 0
  fi
  sleep 5
done

echo "ERROR: Langfuse not healthy after ${WAIT_SEC}s"
docker compose -f docker-compose.langfuse.yml logs --tail=40 langfuse-web || true
exit 1
