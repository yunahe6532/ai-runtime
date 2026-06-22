#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

PORT="${PORT:-8080}"
BASE_URL="http://localhost:${PORT}/v1"
CTX_SIZE="${CTX_SIZE:-200000}"
OUT_DIR="${RUNTIME_DIR}/tmp/dram-200k-bench"
mkdir -p "${OUT_DIR}"

# shellcheck disable=SC1091
source ".env"

echo "=== DRAM KV ctx=${CTX_SIZE} 요청 크기별 벤치 ==="
free -h | tee "${OUT_DIR}/mem-before.txt"

docker compose down >/dev/null 2>&1 || true
CONTEXT_SIZE="${CTX_SIZE}" \
PARALLEL=1 \
NO_KV_OFFLOAD=1 \
CACHE_TYPE_K=q8_0 \
CACHE_TYPE_V=q8_0 \
CACHE_RAM=0 \
docker compose up -d --force-recreate

ready=0
for _ in $(seq 1 180); do
  if curl -fsS "${BASE_URL}/models" > "${OUT_DIR}/models.json" 2>"${OUT_DIR}/models.err"; then
    ready=1
    break
  fi
  sleep 3
done

docker compose logs --no-color --tail=120 llama-server > "${OUT_DIR}/startup.log" || true

if [[ "${ready}" -ne 1 ]]; then
  echo "서버 기동 실패"
  rg -i "OOM|out of memory|cudaMalloc|kv cache|failed" "${OUT_DIR}/startup.log" | tail -n 20 || true
  exit 1
fi

MODEL_ID="$(python3 -c 'import json; print(json.load(open("'"${OUT_DIR}"'/models.json"))["data"][0]["id"])')"
N_CTX="$(python3 -c 'import json; print(json.load(open("'"${OUT_DIR}"'/models.json"))["data"][0]["meta"]["n_ctx"])')"
echo "model=${MODEL_ID}, n_ctx=${N_CTX}"

python3 - <<'PY' > "${OUT_DIR}/results.json"
import json
print(json.dumps({"ctx": None, "runs": []}, ensure_ascii=False))
PY

# label:max_tokens:prompt_chars
CASES=(
  "tiny:64:short"
  "small:256:short"
  "medium:512:short"
  "large:1000:short"
  "xlarge:2000:short"
  "xxlarge:3000:short"
  "large_prompt:512:long"
)

run_case() {
  local label="$1" max_tokens="$2" prompt_kind="$3"
  local run_id="${label}_mt${max_tokens}"
  local prompt

  if [[ "${prompt_kind}" == "long" ]]; then
    prompt="$(python3 - <<'PY'
text = []
for i in range(1, 41):
    text.append(
        f"[파일{i}] 모듈 경계 정리, 예외 처리, 테스트 전략, 롤백 전략, 성능 병목, API 계약, 데이터 마이그레이션, 보안 점검 항목을 포함한 리팩터링 메모 {i}."
    )
print("\n".join(text))
PY
)"
  else
    prompt="요청 크기 벤치마크용으로 한국어 3문장 응답을 작성해줘."
  fi

  echo "==> ${run_id} (prompt_kind=${prompt_kind})"
  nvidia-smi --query-gpu=index,memory.used,utilization.gpu,power.draw --format=csv,noheader,nounits -l 1 > "${OUT_DIR}/${run_id}.smi.csv" &
  local smi_pid=$!

  python3 - "${BASE_URL}" "${MODEL_ID}" "${OUT_DIR}/${run_id}.response.json" "${max_tokens}" "${prompt}" "${OUT_DIR}/results.json" "${label}" "${prompt_kind}" <<'PY'
import json, sys, time, urllib.request
base_url, model_id, out_file, max_tokens, prompt, results_path, label, prompt_kind = sys.argv[1:]
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
with urllib.request.urlopen(req, timeout=3600) as r:
  body = r.read()
t1 = time.perf_counter()
d = json.loads(body.decode("utf-8"))
tim = d.get("timings", {})
usage = d.get("usage", {})
row = {
  "label": label,
  "prompt_kind": prompt_kind,
  "max_tokens": int(max_tokens),
  "prompt_tokens": usage.get("prompt_tokens", 0),
  "completion_tokens": usage.get("completion_tokens", 0),
  "elapsed_s": t1 - t0,
  "gen_tok_s": float(tim.get("predicted_per_second", 0.0)),
  "prompt_tok_s": float(tim.get("prompt_per_second", 0.0)),
  "ttft_ms": float(tim.get("prompt_ms", 0.0)) + float(tim.get("predicted_per_token_ms", 0.0)),
  "prompt_ms": float(tim.get("prompt_ms", 0.0)),
  "gen_ms": float(tim.get("predicted_ms", 0.0)),
}
json.dump(d, open(out_file, "w", encoding="utf-8"), indent=2)
data = json.load(open(results_path, encoding="utf-8"))
data["runs"].append(row)
json.dump(data, open(results_path, "w", encoding="utf-8"), indent=2)
print(json.dumps(row, ensure_ascii=False))
PY

  kill "${smi_pid}" >/dev/null 2>&1 || true
  wait "${smi_pid}" 2>/dev/null || true

  python3 - "${OUT_DIR}/${run_id}.smi.csv" "${OUT_DIR}/results.json" "${label}" <<'PY'
import csv, json, sys
smi_path, results_path, label = sys.argv[1:]
gpu = {0: {"mem": 0.0, "util": 0.0, "power": 0.0}, 1: {"mem": 0.0, "util": 0.0, "power": 0.0}}
with open(smi_path, encoding="utf-8") as f:
  for row in csv.reader(f):
    if len(row) < 4:
      continue
    i = int(row[0].strip())
    if i not in gpu:
      continue
    gpu[i]["mem"] = max(gpu[i]["mem"], float(row[1].strip()))
    gpu[i]["util"] = max(gpu[i]["util"], float(row[2].strip()))
    gpu[i]["power"] = max(gpu[i]["power"], float(row[3].strip()))
data = json.load(open(results_path, encoding="utf-8"))
for r in data["runs"]:
  if r["label"] == label and "gpu0_vram_mib" not in r:
    r["gpu0_vram_mib"] = gpu[0]["mem"]
    r["gpu1_vram_mib"] = gpu[1]["mem"]
    r["gpu0_util"] = gpu[0]["util"]
    r["gpu1_util"] = gpu[1]["util"]
json.dump(data, open(results_path, "w", encoding="utf-8"), indent=2)
PY
}

for case in "${CASES[@]}"; do
  IFS=':' read -r label max_tokens prompt_kind <<< "${case}"
  run_case "${label}" "${max_tokens}" "${prompt_kind}" || true
done

free -h > "${OUT_DIR}/mem-after.txt"

python3 - "${OUT_DIR}/results.json" "${N_CTX}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
ctx = sys.argv[2]
runs = data.get("runs", [])
print(f"\nctx={ctx}, DRAM KV (no-kv-offload, q8, parallel=1)\n")
print("| 요청 | max_tokens | prompt_tokens | completion_tokens | gen tok/s | prompt tok/s | TTFT(ms) | total(s) | GPU0 | GPU1 |")
print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
for r in runs:
  print(
    f"| {r['label']} | {r['max_tokens']} | {r.get('prompt_tokens',0)} | {r.get('completion_tokens',0)} | "
    f"{r.get('gen_tok_s',0):.2f} | {r.get('prompt_tok_s',0):.2f} | {r.get('ttft_ms',0):.1f} | "
    f"{r.get('elapsed_s',0):.2f} | {r.get('gpu0_vram_mib',0):.0f} | {r.get('gpu1_vram_mib',0):.0f} |"
  )
if len(runs) >= 2:
  base = runs[0]
  print(f"\n기준({base['label']}) 대비 gen tok/s 변화:")
  for r in runs[1:]:
    delta = ((r["gen_tok_s"] - base["gen_tok_s"]) / base["gen_tok_s"] * 100.0) if base["gen_tok_s"] else 0.0
    print(f"- {r['label']}: {delta:+.1f}%")
print(f"\n[raw] tmp/dram-200k-bench/results.json")
PY
