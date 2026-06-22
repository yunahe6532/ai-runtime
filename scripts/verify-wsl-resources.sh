#!/usr/bin/env bash
set -euo pipefail

echo "=== WSL 리소스 확인 ==="
echo "CPU (nproc): $(nproc)"
echo "MemTotal: $(awk '/MemTotal/ {printf "%.1f GiB\n", $2/1024/1024}' /proc/meminfo)"
echo "MemAvailable: $(awk '/MemAvailable/ {printf "%.1f GiB\n", $2/1024/1024}' /proc/meminfo)"
echo "SwapTotal: $(awk '/SwapTotal/ {printf "%.1f GiB\n", $2/1024/1024}' /proc/meminfo)"
echo ""
echo "기대값 (권장 설정): processors=16, memory=48GB, swap=16GB"
echo ""

nproc_val="$(nproc)"
mem_gib="$(awk '/MemTotal/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)"
swap_gib="$(awk '/SwapTotal/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)"

ok=1
if [[ "${nproc_val}" -lt 12 ]]; then
  echo "[WARN] CPU 논리 프로세서가 ${nproc_val}개입니다. .wslconfig 적용 후 wsl --shutdown 필요."
  ok=0
fi
if [[ "${mem_gib}" -lt 40 ]]; then
  echo "[WARN] 메모리가 약 ${mem_gib}GB입니다. .wslconfig 적용 후 wsl --shutdown 필요."
  ok=0
fi
if [[ "${swap_gib}" -lt 12 ]]; then
  echo "[WARN] Swap이 약 ${swap_gib}GB입니다. .wslconfig 적용 후 wsl --shutdown 필요."
  ok=0
fi

if [[ "${ok}" -eq 1 ]]; then
  echo "[OK] WSL 리소스가 권장 범위에 있습니다."
else
  echo ""
  echo "Windows PowerShell에서 실행:"
  echo "  wsl --shutdown"
  echo "그 다음 WSL/Cursor 터미널을 다시 열고 이 스크립트를 재실행하세요."
fi
