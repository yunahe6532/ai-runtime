#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

if [[ -f ".env" ]]; then
  # shellcheck disable=SC1091
  source ".env"
fi

PORT="${PORT:-8080}"
BASE_URL="http://localhost:${PORT}/v1"

echo "[1/2] /v1/models 확인"
MODELS_JSON="$(curl -sS "${BASE_URL}/models")"
echo "${MODELS_JSON}"

MODEL_ID="$(python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print((d.get("data") or [{}])[0].get("id",""))' <<< "${MODELS_JSON}")"

if [[ -z "${MODEL_ID}" ]]; then
  echo "모델 ID를 찾지 못했습니다. 서버 또는 모델 상태를 확인하세요."
  exit 1
fi

echo "[2/3] 텍스트 요청 테스트 (model=${MODEL_ID})"
curl -sS "${BASE_URL}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy-key" \
  -d "{
    \"model\": \"${MODEL_ID}\",
    \"messages\": [{\"role\":\"user\",\"content\":\"Say hello in Korean in one short sentence.\"}],
    \"temperature\": 0.2,
    \"max_tokens\": 64
  }"
echo

echo "[3/3] 이미지 요청 테스트 (model=${MODEL_ID})"
curl -sS "${BASE_URL}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy-key" \
  -d "{
    \"model\": \"${MODEL_ID}\",
    \"messages\": [{
      \"role\":\"user\",
      \"content\":[
        {\"type\":\"text\",\"text\":\"Describe this image in one short Korean sentence.\"},
        {\"type\":\"image_url\",\"image_url\":{\"url\":\"https://cdn.britannica.com/61/93061-050-99147DCE/Statue-of-Liberty-Island-New-York-Bay.jpg\"}}
      ]
    }],
    \"temperature\": 0.2,
    \"max_tokens\": 96
  }"
echo
