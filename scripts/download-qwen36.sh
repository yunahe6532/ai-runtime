#!/usr/bin/env bash
# Download Qwen3.6-35B-A3B UD-Q4_K_M + mmproj-F16 (unsloth/Qwen3.6-35B-A3B-GGUF — NOT MTP)
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

PROFILES_FILE="configs/model-profiles.env"
PREFIX="PROFILE_qwen3_6_"

get_var() {
  grep -m1 "^${PREFIX}${1}=" "${PROFILES_FILE}" | cut -d= -f2-
}

CODER_MODEL_FILE="$(get_var CODER_MODEL_FILE)"
CODER_MODEL_URL="$(get_var CODER_MODEL_URL)"
MMPROJ_FILE="$(get_var MMPROJ_FILE)"
MMPROJ_URL="$(get_var MMPROJ_URL)"

CODER_MODEL_FILE="${CODER_MODEL_FILE/#\~/$HOME}"
MMPROJ_FILE="${MMPROJ_FILE/#\~/$HOME}"

mkdir -p "$(dirname "${CODER_MODEL_FILE}")" "$(dirname "${MMPROJ_FILE}")"

download_one() {
  local target="$1" url="$2" label="$3" min_bytes="$4"
  if [[ -f "${target}" ]]; then
    local sz
    sz="$(stat -c%s "${target}" 2>/dev/null || echo 0)"
    if [[ "${sz}" -gt "${min_bytes}" ]]; then
      echo "이미 존재(${label}): ${target} ($(numfmt --to=iec "${sz}" 2>/dev/null || echo "${sz}"))"
      return 0
    fi
  fi
  echo "다운로드(${label}): ${url}"
  echo "  → ${target}"
  if command -v aria2c >/dev/null 2>&1; then
    aria2c --continue=true --max-connection-per-server=8 --split=8 \
      --dir="$(dirname "${target}")" --out="$(basename "${target}")" "${url}"
  elif command -v wget >/dev/null 2>&1; then
    wget -c -O "${target}" "${url}"
  else
    curl -L -C - "${url}" -o "${target}"
  fi
  ls -lh "${target}"
}

echo "=== Qwen3.6-35B-A3B UD-Q4_K_M 다운로드 ==="
download_one "${CODER_MODEL_FILE}" "${CODER_MODEL_URL}" "model" 20000000000
download_one "${MMPROJ_FILE}" "${MMPROJ_URL}" "mmproj" 500000000
echo "완료. 교체: ./scripts/switch-model.sh qwen3_6"
