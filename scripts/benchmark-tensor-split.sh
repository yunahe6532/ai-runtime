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
MAIN_GPU="${MAIN_GPU:-1}"
SPLITS=("20,80" "25,75" "30,70" "35,65" "40,60")
CONTEXTS=("4096")

RESULTS_DIR="${RUNTIME_DIR}/tmp/tensor-split-bench"
mkdir -p "${RESULTS_DIR}"

wait_ready() {
  local retries=120
  for _ in $(seq 1 "${retries}"); do
    if curl -fsS "${BASE_URL}/models" >"${RESULTS_DIR}/models.json" 2>"${RESULTS_DIR}/models.err"; then
      return 0
    fi
    sleep 2
  done
  return 1
}

echo "# tensor-split benchmark"
echo "contexts: ${CONTEXTS[*]}"
echo "splits: ${SPLITS[*]}"
echo "main-gpu: ${MAIN_GPU}"

echo "CUDA device mapping:"
nvidia-smi -L | tee "${RESULTS_DIR}/gpu-physical-map.txt"

python3 - <<'PY' > "${RESULTS_DIR}/results.json"
import json
print(json.dumps({"runs": []}, ensure_ascii=False))
PY

for ctx in "${CONTEXTS[@]}"; do
  for split in "${SPLITS[@]}"; do
    run_id="ctx${ctx}_split${split//,/}"
    echo ""
    echo "==> run: ${run_id}"

    docker compose down >/dev/null 2>&1 || true
    CONTEXT_SIZE="${ctx}" SPLIT_MODE="layer" MAIN_GPU="${MAIN_GPU}" TENSOR_SPLIT="${split}" docker compose up -d --force-recreate >/dev/null

    if ! wait_ready; then
      echo "서버 기동 실패: ${run_id}"
      docker compose logs --no-color --tail=300 llama-server > "${RESULTS_DIR}/${run_id}.startup.log" || true
      if rg -i "out of memory|cuda.*malloc|failed|OOM" "${RESULTS_DIR}/${run_id}.startup.log" >/dev/null; then
        echo "OOM 기록: ${run_id}"
        python3 - "${RESULTS_DIR}/results.json" "${ctx}" "${split}" "${RESULTS_DIR}/${run_id}.startup.log" <<'PY'
import json, sys
from pathlib import Path
rp = Path(sys.argv[1]); ctx=int(sys.argv[2]); split=sys.argv[3]; lp=Path(sys.argv[4])
d=json.loads(rp.read_text(encoding="utf-8"))
txt=lp.read_text(encoding="utf-8", errors="ignore")
cuda_map=[line.strip() for line in txt.splitlines() if "CUDA0" in line or "CUDA1" in line]
d["runs"].append({"context":ctx,"tensor_split":split,"status":"oom","cuda_mapping":cuda_map})
rp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
PY
      fi
      continue
    fi

    docker compose logs --no-color --tail=400 llama-server > "${RESULTS_DIR}/${run_id}.startup.log" || true

    MODEL_ID="$(python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print((d.get("data") or [{}])[0].get("id",""))' < "${RESULTS_DIR}/models.json")"
    if [[ -z "${MODEL_ID}" ]]; then
      echo "모델 ID 조회 실패: ${run_id}"
      continue
    fi

    SMI_RAW="${RESULTS_DIR}/${run_id}.smi.csv"
    nvidia-smi --query-gpu=index,memory.used,utilization.gpu,power.draw,temperature.gpu --format=csv,noheader,nounits -l 1 > "${SMI_RAW}" &
    SMI_PID=$!

    python3 - "${BASE_URL}" "${MODEL_ID}" "${RESULTS_DIR}/${run_id}.response.json" <<'PY'
import json
import sys
import time
import urllib.request

base_url, model_id, out_file = sys.argv[1], sys.argv[2], sys.argv[3]
url = f"{base_url}/chat/completions"
payload = {
    "model": model_id,
    "messages": [
        {"role": "system", "content": "You are a concise assistant."},
        {"role": "user", "content": "로컬 LLM 듀얼 GPU tensor-split 성능 측정을 위한 2문장 답변을 작성해줘."},
    ],
    "temperature": 0.2,
    "max_tokens": 192,
    "stream": False,
}
data = json.dumps(payload).encode("utf-8")
req = urllib.request.Request(
    url,
    data=data,
    headers={"Content-Type": "application/json", "Authorization": "Bearer dummy-key"},
    method="POST",
)
t0 = time.perf_counter()
with urllib.request.urlopen(req, timeout=600) as resp:
    body = resp.read()
t1 = time.perf_counter()
result = json.loads(body.decode("utf-8"))
result["_elapsed_s"] = t1 - t0
with open(out_file, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
PY

    kill "${SMI_PID}" >/dev/null 2>&1 || true
    wait "${SMI_PID}" 2>/dev/null || true

    python3 - "${RESULTS_DIR}/results.json" "${RESULTS_DIR}/${run_id}.response.json" "${SMI_RAW}" "${RESULTS_DIR}/${run_id}.startup.log" "${ctx}" "${split}" <<'PY'
import csv
import json
import re
import sys
from pathlib import Path

results_path = Path(sys.argv[1])
resp_path = Path(sys.argv[2])
smi_path = Path(sys.argv[3])
log_path = Path(sys.argv[4])
ctx = int(sys.argv[5])
split = sys.argv[6]

results = json.loads(results_path.read_text(encoding="utf-8"))
resp = json.loads(resp_path.read_text(encoding="utf-8"))
tim = resp.get("timings", {})

prompt_tok_s = tim.get("prompt_per_second", 0.0)
gen_tok_s = tim.get("predicted_per_second", 0.0)
ttft_ms = float(tim.get("prompt_ms", 0.0)) + float(tim.get("predicted_per_token_ms", 0.0))
tok_s = gen_tok_s

gpu_stats = {
    "0": {"mem": 0.0, "util": 0.0, "power": 0.0, "temp": 0.0},
    "1": {"mem": 0.0, "util": 0.0, "power": 0.0, "temp": 0.0},
}
if smi_path.exists():
    with smi_path.open("r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 5:
                continue
            idx = row[0].strip()
            if idx not in gpu_stats:
                continue
            try:
                mem = float(row[1].strip())
                util = float(row[2].strip())
                power = float(row[3].strip())
                temp = float(row[4].strip())
            except ValueError:
                continue
            gpu_stats[idx]["mem"] = max(gpu_stats[idx]["mem"], mem)
            gpu_stats[idx]["util"] = max(gpu_stats[idx]["util"], util)
            gpu_stats[idx]["power"] = max(gpu_stats[idx]["power"], power)
            gpu_stats[idx]["temp"] = max(gpu_stats[idx]["temp"], temp)

log_text = log_path.read_text(encoding="utf-8", errors="ignore") if log_path.exists() else ""
patterns = [
    r".*offload.*",
    r".*CUDA.*buffer.*",
    r".*KV cache.*",
    r".*compute buffer.*",
    r".*workspace.*",
]
log_hits = []
for line in log_text.splitlines():
    for p in patterns:
        if re.search(p, line, flags=re.IGNORECASE):
            log_hits.append(line.strip())
            break
if len(log_hits) > 20:
    log_hits = log_hits[-20:]
cuda_mapping = [line.strip() for line in log_text.splitlines() if "CUDA0" in line or "CUDA1" in line]

results["runs"].append(
    {
        "status": "ok",
        "context": ctx,
        "tensor_split": split,
        "tokens_per_sec": tok_s,
        "ttft_ms": ttft_ms,
        "prompt_eval_tok_s": prompt_tok_s,
        "generation_tok_s": gen_tok_s,
        "gpu0": gpu_stats["0"],
        "gpu1": gpu_stats["1"],
        "cuda_mapping": cuda_mapping,
        "log_hits": log_hits,
    }
)
results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
PY

    echo "완료: ${run_id}"
  done
done

python3 - "${RESULTS_DIR}/results.json" <<'PY'
import json
import sys
from collections import defaultdict

data = json.load(open(sys.argv[1], encoding="utf-8"))
runs = data.get("runs", [])
if not runs:
    print("결과 없음")
    raise SystemExit(1)

print("")
print("| context | tensor-split | status | tok/s | TTFT(ms) | prompt tok/s | gen tok/s | GPU0 VRAM(MiB) | GPU1 VRAM(MiB) | GPU0 Util(%) | GPU1 Util(%) | GPU0 Power(W) | GPU1 Power(W) | GPU0 Temp(C) | GPU1 Temp(C) |")
print("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
for r in sorted(runs, key=lambda x: (x.get("context", 0), x.get("tensor_split", ""))):
    if r.get("status") != "ok":
        print(f'| {r.get("context","")} | {r.get("tensor_split","")} | {r.get("status","")} | - | - | - | - | - | - | - | - | - | - | - | - |')
        continue
    print(
        f'| {r["context"]} | {r["tensor_split"]} | {r["status"]} | {r["tokens_per_sec"]:.2f} | {r["ttft_ms"]:.2f} | '
        f'{r["prompt_eval_tok_s"]:.2f} | {r["generation_tok_s"]:.2f} | '
        f'{r["gpu0"]["mem"]:.0f} | {r["gpu1"]["mem"]:.0f} | '
        f'{r["gpu0"]["util"]:.0f} | {r["gpu1"]["util"]:.0f} | '
        f'{r["gpu0"]["power"]:.0f} | {r["gpu1"]["power"]:.0f} | '
        f'{r["gpu0"]["temp"]:.0f} | {r["gpu1"]["temp"]:.0f} |'
    )

by_ctx = defaultdict(list)
for r in runs:
    if r.get("status") == "ok":
        by_ctx[r["context"]].append(r)
for ctx, items in sorted(by_ctx.items()):
    best = max(items, key=lambda x: x["tokens_per_sec"])
    stable = min(items, key=lambda x: (x["gpu0"]["util"] + x["gpu1"]["util"], x["gpu0"]["power"] + x["gpu1"]["power"], -x["tokens_per_sec"]))
    print(f"\n[context {ctx}] fastest={best['tensor_split']} ({best['tokens_per_sec']:.2f} tok/s), stable={stable['tensor_split']}")

print("\n로그 추출 파일:")
print("tmp/tensor-split-bench/*.startup.log")
print("JSON 결과 파일:")
print("tmp/tensor-split-bench/results.json")
print("GPU 물리 매핑:")
print("tmp/tensor-split-bench/gpu-physical-map.txt")
PY
