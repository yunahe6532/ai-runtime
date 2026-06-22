#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

PORT="${PORT:-8080}"
BASE_URL="http://localhost:${PORT}/v1"
REPEATS=3
PROMPT="로컬 LLM 벤치마크를 위해 동일한 포맷으로 한국어 3문장 응답을 작성해줘."
LONG_PROMPT="너는 시니어 소프트웨어 엔지니어다. 아래 요구사항을 만족하는 리팩터링 계획과 코드 변경안을 한국어로 작성하라. 1) 모듈 경계 정리 2) 예외 처리 강화 3) 테스트 전략 제시 4) 잠재 리스크와 롤백 전략 포함. 마지막에 체크리스트를 제공하라."

OUT_DIR="${RUNTIME_DIR}/tmp/parallel-bench"
mkdir -p "${OUT_DIR}"

# shellcheck disable=SC1091
source ".env"

wait_ready() {
  for _ in $(seq 1 120); do
    if curl -fsS "${BASE_URL}/models" > "${OUT_DIR}/models.json" 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

run_bench() {
  local parallel_label="$1"
  local parallel_val="$2"
  local max_tokens="$3"
  local prompt="$4"
  local run_idx="$5"
  local run_id="p${parallel_label}_mt${max_tokens}_r${run_idx}"

  docker compose down >/dev/null 2>&1 || true
  if [[ -n "${parallel_val}" ]]; then
    PARALLEL="${parallel_val}" docker compose up -d --force-recreate >/dev/null
  else
    PARALLEL="" docker compose up -d --force-recreate >/dev/null
  fi

  if ! wait_ready; then
    echo "{\"parallel\":\"${parallel_label}\",\"status\":\"fail\",\"run\":${run_idx}}" >> "${OUT_DIR}/results.jsonl"
    return 1
  fi

  docker compose logs --no-color --tail=80 llama-server | rg "n_parallel|parallel" | tail -n 2 > "${OUT_DIR}/${run_id}.parallel.log" || true

  local model_id
  model_id="$(python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print((d.get("data") or [{}])[0].get("id",""))' < "${OUT_DIR}/models.json")"

  nvidia-smi --query-gpu=index,memory.used,utilization.gpu,power.draw --format=csv,noheader,nounits -l 1 > "${OUT_DIR}/${run_id}.smi.csv" &
  local smi_pid=$!

  python3 - "${BASE_URL}" "${model_id}" "${OUT_DIR}/${run_id}.response.json" "${max_tokens}" "${prompt}" <<'PY'
import json, sys, time, urllib.request
base_url, model_id, out_file, max_tokens, prompt = sys.argv[1:]
payload = {
  "model": model_id,
  "messages": [{"role": "user", "content": prompt}],
  "temperature": 0.2,
  "max_tokens": int(max_tokens),
  "stream": False,
}
req = urllib.request.Request(
  f"{base_url}/chat/completions",
  data=json.dumps(payload).encode("utf-8"),
  headers={"Content-Type": "application/json", "Authorization": "Bearer dummy-key"},
  method="POST",
)
t0 = time.perf_counter()
with urllib.request.urlopen(req, timeout=1800) as r:
  body = r.read()
t1 = time.perf_counter()
d = json.loads(body.decode("utf-8"))
tim = d.get("timings", {})
out = {
  "elapsed_s": t1 - t0,
  "gen_tok_s": tim.get("predicted_per_second", 0.0),
  "prompt_tok_s": tim.get("prompt_per_second", 0.0),
  "ttft_ms": float(tim.get("prompt_ms", 0.0)) + float(tim.get("predicted_per_token_ms", 0.0)),
  "completion_tokens": d.get("usage", {}).get("completion_tokens", 0),
}
json.dump(out, open(out_file, "w", encoding="utf-8"), indent=2)
print(json.dumps(out, ensure_ascii=False))
PY

  kill "${smi_pid}" >/dev/null 2>&1 || true
  wait "${smi_pid}" 2>/dev/null || true

  python3 - "${OUT_DIR}/${run_id}.response.json" "${OUT_DIR}/${run_id}.smi.csv" "${parallel_label}" "${run_idx}" "${max_tokens}" <<'PY'
import csv, json, sys
resp = json.load(open(sys.argv[1], encoding="utf-8"))
parallel, run_idx, max_tokens = sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
gpu = {0: {"mem": 0.0, "power": 0.0}, 1: {"mem": 0.0, "power": 0.0}}
with open(sys.argv[2], encoding="utf-8") as f:
  for row in csv.reader(f):
    if len(row) < 4:
      continue
    i = int(row[0].strip())
    if i not in gpu:
      continue
    gpu[i]["mem"] = max(gpu[i]["mem"], float(row[1].strip()))
    gpu[i]["power"] = max(gpu[i]["power"], float(row[3].strip()))
row = {
  "parallel": parallel,
  "run": run_idx,
  "max_tokens": max_tokens,
  "status": "ok",
  **resp,
  "gpu0_vram_mib": gpu[0]["mem"],
  "gpu1_vram_mib": gpu[1]["mem"],
  "gpu0_power_w": gpu[0]["power"],
  "gpu1_power_w": gpu[1]["power"],
}
with open("tmp/parallel-bench/results.jsonl", "a", encoding="utf-8") as f:
  f.write(json.dumps(row, ensure_ascii=False) + "\n")
PY
}

: > "${OUT_DIR}/results.jsonl"

echo "=== parallel 비교 (ctx=${CONTEXT_SIZE}, split=${TENSOR_SPLIT}) ==="

for i in $(seq 1 "${REPEATS}"); do
  echo "-- auto(4) short #${i}"
  run_bench "auto4" "" 220 "${PROMPT}" "${i}" || true
done

for i in $(seq 1 "${REPEATS}"); do
  echo "-- parallel 1 short #${i}"
  run_bench "1" "1" 220 "${PROMPT}" "${i}" || true
done

for i in $(seq 1 2); do
  echo "-- auto(4) long #${i}"
  run_bench "auto4" "" 1500 "${LONG_PROMPT}" "${i}" || true
done

for i in $(seq 1 2); do
  echo "-- parallel 1 long #${i}"
  run_bench "1" "1" 1500 "${LONG_PROMPT}" "${i}" || true
done

python3 - "${OUT_DIR}/results.jsonl" <<'PY'
import json, sys
from collections import defaultdict

rows = []
with open(sys.argv[1], encoding="utf-8") as f:
  for line in f:
    line = line.strip()
    if not line:
      continue
    rows.append(json.loads(line))

grp = defaultdict(list)
for r in rows:
  if r.get("status") != "ok":
    continue
  grp[(r["parallel"], r["max_tokens"])].append(r)

def avg(vals):
  return sum(vals) / len(vals) if vals else 0.0

print("")
print("| parallel | max_tokens | runs | gen tok/s | prompt tok/s | TTFT(ms) | elapsed(s) | GPU0 VRAM | GPU1 VRAM |")
print("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
for key in sorted(grp.keys()):
  p, mt = key
  items = grp[key]
  print(
    f"| {p} | {mt} | {len(items)} | {avg([x['gen_tok_s'] for x in items]):.2f} | "
    f"{avg([x['prompt_tok_s'] for x in items]):.2f} | {avg([x['ttft_ms'] for x in items]):.2f} | "
    f"{avg([x['elapsed_s'] for x in items]):.2f} | {avg([x['gpu0_vram_mib'] for x in items]):.0f} MiB | "
    f"{avg([x['gpu1_vram_mib'] for x in items]):.0f} MiB |"
  )

short4 = grp.get(("auto4", 220), [])
short1 = grp.get(("1", 220), [])
if short4 and short1:
  g4 = avg([x["gen_tok_s"] for x in short4])
  g1 = avg([x["gen_tok_s"] for x in short1])
  delta = ((g1 - g4) / g4 * 100.0) if g4 else 0.0
  print(f"\nshort gen tok/s delta (p1 vs auto4): {delta:+.1f}%")

long4 = grp.get(("auto4", 1500), [])
long1 = grp.get(("1", 1500), [])
if long4 and long1:
  g4 = avg([x["gen_tok_s"] for x in long4])
  g1 = avg([x["gen_tok_s"] for x in long1])
  delta = ((g1 - g4) / g4 * 100.0) if g4 else 0.0
  print(f"long gen tok/s delta (p1 vs auto4): {delta:+.1f}%")
PY
