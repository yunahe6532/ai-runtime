#!/usr/bin/env bash
# Full model swap workflow: baseline → download → swap → benchmark
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

BENCH_OUT="tmp/benchmark-cursor-agent.json"
PROFILE_TARGET="${1:-qwen3_6}"

chmod +x scripts/switch-model.sh scripts/download-qwen36.sh scripts/benchmark-cursor-agent.py

echo "=== 1) Baseline (current profile) ==="
python3 scripts/benchmark-cursor-agent.py --label before --out "${BENCH_OUT}" || true

echo ""
echo "=== 2) Download Qwen3.6 (if missing) ==="
./scripts/download-qwen36.sh

echo ""
echo "=== 3) Switch profile → ${PROFILE_TARGET} ==="
./scripts/switch-model.sh "${PROFILE_TARGET}"

echo "Waiting for long backend (up to 5min)..."
for i in $(seq 1 60); do
  if curl -fsS http://localhost:8082/v1/models >/dev/null 2>&1; then
    echo "llama-long ready"
    break
  fi
  sleep 5
  echo "  ...${i}0s"
done

echo ""
echo "=== 4) Post-swap benchmark ==="
python3 scripts/benchmark-cursor-agent.py --label after --out "${BENCH_OUT}" --compare "${BENCH_OUT}" || true

echo ""
echo "=== Done ==="
echo "Results: ${BENCH_OUT}"
python3 - "${BENCH_OUT}" <<'PY'
import json, sys
d=json.load(open(sys.argv[1]))
runs=d.get("runs",[])
for r in runs[-2:]:
    s=r["summary"]
    print(f"\n[{r.get('label')}] {r.get('model_label')}")
    print(f"  pass={s.get('passed')}/{s.get('total')} tool_match={s.get('tool_match_rate')}%")
    print(f"  avg_wall={s.get('avg_wall_ms')}ms gen_tps={s.get('avg_gen_tps')}")
PY
