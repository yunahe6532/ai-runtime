#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

if [[ ! -f ".env" ]]; then
  echo ".env 파일이 없습니다. .env.example을 복사해 생성하세요."
  exit 1
fi

# shellcheck disable=SC1091
source ".env"

MODEL_FILE_EXPANDED="${MODEL_FILE:-}"
MODEL_FILE_EXPANDED="${MODEL_FILE_EXPANDED/#\~/$HOME}"
MMPROJ_FILE_EXPANDED="${MMPROJ_FILE:-}"
MMPROJ_FILE_EXPANDED="${MMPROJ_FILE_EXPANDED/#\~/$HOME}"

if [[ -z "${MODEL_FILE_EXPANDED}" ]]; then
  echo "MODEL_FILE 값이 비어 있습니다."
  exit 1
fi

if [[ ! -f "${MODEL_FILE_EXPANDED}" ]]; then
  echo "모델 파일 없음: ${MODEL_FILE_EXPANDED}"
  exit 1
fi

if [[ -n "${MMPROJ_FILE_EXPANDED}" && ! -f "${MMPROJ_FILE_EXPANDED}" ]]; then
  echo "mmproj 파일 없음: ${MMPROJ_FILE_EXPANDED}"
  exit 1
fi

export MODEL_FILE="${MODEL_FILE_EXPANDED}"
export MMPROJ_FILE="${MMPROJ_FILE_EXPANDED}"

# 기존 단일 컨테이너 정리
docker rm -f cursor-local-llm 2>/dev/null || true

docker compose build router
docker compose up -d router llama-fast
docker compose create llama-long >/dev/null 2>&1 || true
docker stop cursor-local-llm-long >/dev/null 2>&1 || true

docker compose ps

echo "Router 상태:"
curl -fsS "http://localhost:${PORT:-8080}/router/status" | python3 -m json.tool || true
echo ""
echo "API 확인: http://localhost:${PORT:-8080}/v1/models"
curl -fsS "http://localhost:${PORT:-8080}/v1/models" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("n_ctx", d["data"][0]["meta"]["n_ctx"])' || true
