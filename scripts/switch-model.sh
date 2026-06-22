#!/usr/bin/env bash
# Apply a model profile from configs/model-profiles.env and restart llama backends.
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
PROFILES_FILE="${RUNTIME_DIR}/configs/model-profiles.env"
ENV_FILE="${RUNTIME_DIR}/.env"
cd "${RUNTIME_DIR}"

usage() {
  echo "사용법: $0 <profile> [--no-restart]"
  echo ""
  echo "프로필:"
  grep -E '^PROFILE_[a-zA-Z0-9_]+_LABEL=' "${PROFILES_FILE}" 2>/dev/null | while read -r line; do
    key="${line#PROFILE_}"
    key="${key%%_LABEL=*}"
    label="${line#*=}"
    echo "  ${key}  — ${label}"
  done
  echo ""
  echo "현재 ACTIVE_PROFILE: $(grep '^ACTIVE_PROFILE=' "${PROFILES_FILE}" 2>/dev/null | cut -d= -f2- || echo '?')"
}

TARGET="${1:-}"
NO_RESTART=0
if [[ "${2:-}" == "--no-restart" ]]; then NO_RESTART=1; fi

if [[ -z "${TARGET}" ]]; then
  usage
  exit 1
fi

if [[ ! -f "${PROFILES_FILE}" ]]; then
  echo "프로필 파일 없음: ${PROFILES_FILE}"
  exit 1
fi

PREFIX="PROFILE_${TARGET}_"
LABEL="$(grep -m1 "^${PREFIX}LABEL=" "${PROFILES_FILE}" | cut -d= -f2- || true)"
if [[ -z "${LABEL}" ]]; then
  echo "지원하지 않는 프로필: ${TARGET}"
  usage
  exit 1
fi

python3 - "${PROFILES_FILE}" "${ENV_FILE}" "${TARGET}" <<'PY'
import pathlib
import re
import sys

profiles_path = pathlib.Path(sys.argv[1])
env_path = pathlib.Path(sys.argv[2])
profile = sys.argv[3]
prefix = f"PROFILE_{profile}_"

mapping: dict[str, str] = {}
for line in profiles_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith(prefix):
        key = line[len(prefix):].split("=", 1)[0]
        val = line.split("=", 1)[1]
        mapping[key] = val

if not mapping:
    raise SystemExit(f"profile empty: {profile}")

text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""

def set_kv(content: str, key: str, value: str) -> str:
    pat = rf"^{re.escape(key)}=.*$"
    repl = f"{key}={value}"
    if re.search(pat, content, flags=re.MULTILINE):
        return re.sub(pat, repl, content, flags=re.MULTILINE)
    if content and not content.endswith("\n"):
        content += "\n"
    return content + repl + "\n"

for key, val in mapping.items():
    if key == "LABEL":
        continue
    text = set_kv(text, key, val)

text = set_kv(text, "ACTIVE_PROFILE", profile)
env_path.write_text(text, encoding="utf-8")

# update ACTIVE_PROFILE in profiles file too
pt = profiles_path.read_text(encoding="utf-8")
if re.search(r"^ACTIVE_PROFILE=.*$", pt, flags=re.MULTILINE):
    pt = re.sub(r"^ACTIVE_PROFILE=.*$", f"ACTIVE_PROFILE={profile}", pt, flags=re.MULTILINE)
else:
    pt += f"\nACTIVE_PROFILE={profile}\n"
profiles_path.write_text(pt, encoding="utf-8")

print("applied keys:")
for k in sorted(mapping):
    if k != "LABEL":
        print(f"  {k}={mapping[k]}")
PY

echo ""
echo "프로필 적용: ${TARGET} (${LABEL})"

# verify model files exist
# shellcheck disable=SC1091
source "${ENV_FILE}"
CODER_MODEL_FILE="${CODER_MODEL_FILE/#\~/$HOME}"
if [[ ! -f "${CODER_MODEL_FILE}" ]]; then
  echo "WARN: CODER 모델 파일 없음: ${CODER_MODEL_FILE}"
  echo "  → ./scripts/download-qwen36.sh  또는 ./scripts/download-coder-model.sh"
fi

if [[ "${NO_RESTART}" -eq 1 ]]; then
  echo "(--no-restart) 백엔드 재시작 생략"
  exit 0
fi

echo "백엔드 재시작 중..."
docker compose stop llama-fast llama-long llama-vl 2>/dev/null || true

ROUTER_MODE="$(grep -m1 '^ROUTER_MODE=' "${ENV_FILE}" 2>/dev/null | cut -d= -f2- || echo legacy)"
FAST_ENABLED="$(grep -m1 '^FAST_ENABLED=' "${ENV_FILE}" 2>/dev/null | cut -d= -f2- || echo 1)"

if [[ "${ROUTER_MODE}" == "unified" ]] || [[ "${FAST_ENABLED}" == "0" ]]; then
  echo "  unified: long 단일 기동 (fast/vl OFF)"
  docker compose up -d llama-long 2>/dev/null || true
else
  echo "  legacy: long 기동 (fast/vl 필요 시 router가 스위칭)"
  docker compose up -d llama-long 2>/dev/null || true
fi
sleep 2
docker compose up -d router 2>/dev/null || true

echo "완료. 상태 확인: curl -s http://localhost:8080/router/status | python3 -m json.tool"
