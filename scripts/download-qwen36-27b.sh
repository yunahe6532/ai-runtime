#!/usr/bin/env bash
# Download Qwen3.6-27B UD-Q4_K_XL + mmproj-F16 (unsloth/Qwen3.6-27B-GGUF)
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

# .env HF_TOKEN → huggingface-cli / wget 인증 (rate limit 완화)
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
export HUGGING_FACE_HUB_TOKEN="${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}"

PROFILES_FILE="configs/model-profiles.env"
PREFIX="PROFILE_qwen3_6_27b_"

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

hf_repo_from_url() {
  local url="$1"
  if [[ "${url}" =~ huggingface\.co/([^/]+/[^/]+)/resolve/main/([^?]+) ]]; then
    echo "${BASH_REMATCH[1]} ${BASH_REMATCH[2]}"
  fi
}

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

  local partial_sz=0
  if [[ -f "${target}" ]]; then
    partial_sz="$(stat -c%s "${target}" 2>/dev/null || echo 0)"
  fi

  local hf_parts repo_id hf_file
  hf_parts="$(hf_repo_from_url "${url}")"
  # wget 부분 파일 이어받기: HF_TOKEN + wget -c (huggingface-cli는 wget partial과 호환 안 됨)
  if [[ "${partial_sz}" -gt 0 && "${partial_sz}" -lt "${min_bytes}" ]] \
      && [[ -n "${HUGGING_FACE_HUB_TOKEN}" ]] && command -v wget >/dev/null 2>&1; then
    echo "  방법: wget -c 이어받기 (HF_TOKEN, 기존 $(numfmt --to=iec "${partial_sz}" 2>/dev/null || echo "${partial_sz}"))"
    wget -c --header="Authorization: Bearer ${HUGGING_FACE_HUB_TOKEN}" -O "${target}" "${url}"
  elif [[ -n "${HUGGING_FACE_HUB_TOKEN}" && -n "${hf_parts}" ]] && command -v huggingface-cli >/dev/null 2>&1; then
    read -r repo_id hf_file <<< "${hf_parts}"
    echo "  방법: huggingface-cli (HF_TOKEN 인증)"
    huggingface-cli download "${repo_id}" "${hf_file}" \
      --local-dir "$(dirname "${target}")" \
      --token "${HUGGING_FACE_HUB_TOKEN}"
    local fetched="${target}"
    if [[ "$(dirname "${target}")/$(basename "${hf_file}")" != "${target}" ]]; then
      fetched="$(dirname "${target}")/$(basename "${hf_file}")"
      mv -f "${fetched}" "${target}"
    fi
  elif command -v aria2c >/dev/null 2>&1; then
    local auth_args=()
    if [[ -n "${HUGGING_FACE_HUB_TOKEN}" ]]; then
      auth_args=(--header="Authorization: Bearer ${HUGGING_FACE_HUB_TOKEN}")
    fi
    aria2c --continue=true --max-connection-per-server=8 --split=8 \
      "${auth_args[@]}" \
      --dir="$(dirname "${target}")" --out="$(basename "${target}")" "${url}"
  elif command -v wget >/dev/null 2>&1; then
    local auth_args=()
    if [[ -n "${HUGGING_FACE_HUB_TOKEN}" ]]; then
      auth_args=(--header="Authorization: Bearer ${HUGGING_FACE_HUB_TOKEN}")
      echo "  방법: wget -c (HF_TOKEN 인증)"
    fi
    wget -c "${auth_args[@]}" -O "${target}" "${url}"
  else
    local auth_args=()
    if [[ -n "${HUGGING_FACE_HUB_TOKEN}" ]]; then
      auth_args=(-H "Authorization: Bearer ${HUGGING_FACE_HUB_TOKEN}")
    fi
    curl -L -C - "${auth_args[@]}" "${url}" -o "${target}"
  fi
  ls -lh "${target}"
}

echo "=== Qwen3.6-27B UD-Q4_K_XL 다운로드 (~17.6GB + mmproj) ==="
download_one "${CODER_MODEL_FILE}" "${CODER_MODEL_URL}" "model" 15000000000
download_one "${MMPROJ_FILE}" "${MMPROJ_URL}" "mmproj" 400000000
echo "완료. 교체: ./scripts/switch-model.sh qwen3_6_27b"
