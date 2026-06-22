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

CODER_MODEL_FILE="${CODER_MODEL_FILE:-}"
CODER_MODEL_URL="${CODER_MODEL_URL:-}"
CODER_MODEL_FILE="${CODER_MODEL_FILE/#\~/$HOME}"

if [[ -z "${CODER_MODEL_FILE}" || -z "${CODER_MODEL_URL}" ]]; then
  echo "CODER_MODEL_FILE 또는 CODER_MODEL_URL 값이 비어 있습니다."
  exit 1
fi

mkdir -p "$(dirname "${CODER_MODEL_FILE}")"

if [[ -f "${CODER_MODEL_FILE}" ]] && [[ "$(stat -c%s "${CODER_MODEL_FILE}" 2>/dev/null || echo 0)" -gt 1000000000 ]]; then
  echo "이미 존재함: ${CODER_MODEL_FILE}"
  ls -lh "${CODER_MODEL_FILE}"
  exit 0
fi

echo "다운로드: Qwen3-Coder UD-Q4_K_XL (~17.7GB)"
echo "URL: ${CODER_MODEL_URL}"
echo "저장: ${CODER_MODEL_FILE}"

if command -v aria2c >/dev/null 2>&1; then
  aria2c --continue=true --max-connection-per-server=8 --split=8 \
    --dir="$(dirname "${CODER_MODEL_FILE}")" \
    --out="$(basename "${CODER_MODEL_FILE}")" \
    "${CODER_MODEL_URL}"
elif command -v wget >/dev/null 2>&1; then
  wget -c -O "${CODER_MODEL_FILE}" "${CODER_MODEL_URL}"
elif command -v curl >/dev/null 2>&1; then
  curl -L -C - "${CODER_MODEL_URL}" -o "${CODER_MODEL_FILE}"
else
  echo "다운로드 도구 없음: aria2c/wget/curl 중 하나가 필요합니다."
  exit 1
fi

echo "다운로드 완료:"
ls -lh "${CODER_MODEL_FILE}"
