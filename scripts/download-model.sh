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

MODEL_FILE="${MODEL_FILE:-}"
MODEL_URL="${MODEL_URL:-}"
MMPROJ_FILE="${MMPROJ_FILE:-}"
MMPROJ_URL="${MMPROJ_URL:-}"
MODEL_FILE="${MODEL_FILE/#\~/$HOME}"
MMPROJ_FILE="${MMPROJ_FILE/#\~/$HOME}"

if [[ -z "${MODEL_FILE}" || -z "${MODEL_URL}" || -z "${MMPROJ_FILE}" || -z "${MMPROJ_URL}" ]]; then
  echo "MODEL/MMPROJ 파일 경로 또는 URL 값이 비어 있습니다."
  exit 1
fi

mkdir -p "$(dirname "${MODEL_FILE}")" "$(dirname "${MMPROJ_FILE}")"

download_file() {
  local target_file="$1"
  local source_url="$2"
  local label="$3"

  if [[ -f "${target_file}" ]]; then
    echo "이미 존재함(${label}): ${target_file}"
    ls -lh "${target_file}"
    return 0
  fi

  echo "다운로드 시작(${label}): ${source_url}"
  echo "저장 경로: ${target_file}"

  if command -v aria2c >/dev/null 2>&1; then
    if ! aria2c --continue=true --max-connection-per-server=8 --split=8 --dir="$(dirname "${target_file}")" --out="$(basename "${target_file}")" "${source_url}"; then
      echo "aria2c 다운로드 실패(${label})"
      exit 1
    fi
  elif command -v wget >/dev/null 2>&1; then
    if ! wget -O "${target_file}" "${source_url}"; then
      echo "wget 다운로드 실패(${label})"
      exit 1
    fi
  elif command -v curl >/dev/null 2>&1; then
    if ! curl -L "${source_url}" -o "${target_file}"; then
      echo "curl 다운로드 실패(${label})"
      exit 1
    fi
  else
    echo "다운로드 도구 없음: aria2c/wget/curl 중 하나가 필요합니다."
    exit 1
  fi
}

download_file "${MODEL_FILE}" "${MODEL_URL}" "model"
download_file "${MMPROJ_FILE}" "${MMPROJ_URL}" "mmproj"

echo "다운로드 완료(model):"
ls -lh "${MODEL_FILE}"
echo "다운로드 완료(mmproj):"
ls -lh "${MMPROJ_FILE}"
