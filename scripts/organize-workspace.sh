#!/usr/bin/env bash
# Ensure project artifacts stay under cursor-local-llm/tmp (not ~/ root).
set -euo pipefail

ROOT="/home/yunahe/ai-runtime/cursor-local-llm"
TMP="${ROOT}/tmp"
ARCHIVE="${TMP}/archive/home-root-strays"

mkdir -p "${TMP}/cursor-captures" "${TMP}/context-cache" "${TMP}/benchmarks" "${ARCHIVE}"

move_if_exists() {
  local src="$1" dest="$2"
  if [[ -e "${src}" ]]; then
    echo "move: ${src} → ${dest}"
    mv "${src}" "${dest}"
  fi
}

# Stray project-like files sometimes created at ~/ during agent sessions
for name in results.json current_state.json .flow.json benchmark-cursor-agent.json; do
  move_if_exists "/home/yunahe/${name}" "${ARCHIVE}/${name}"
done

# Ensure benchmark default path
if [[ -f "${ROOT}/tmp/benchmark-cursor-agent.json" ]]; then
  :
elif [[ -f "${ARCHIVE}/benchmark-cursor-agent.json" ]]; then
  cp "${ARCHIVE}/benchmark-cursor-agent.json" "${TMP}/benchmark-cursor-agent.json"
fi

echo ""
echo "프로젝트 tmp 구조:"
find "${TMP}" -maxdepth 2 -type d | head -20
echo ""
echo "홈 루트 loose json (있으면 정리 대상):"
find /home/yunahe -maxdepth 1 -type f -name '*.json' 2>/dev/null || true
echo ""
echo "권장 Cursor workspace: ${ROOT}"
echo "모델 경로 (공용): ~/models/ — configs/models.manifest.json 참고"
