#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

PORT="${PORT:-8080}"
BASE_URL="http://localhost:${PORT}/v1"
MAIN_GPU="${MAIN_GPU:-1}"
SPLIT_MODE="${SPLIT_MODE:-layer}"

# 실사용 후보: 기본 50,50@24576 (5회), 비교군 50,50@32768 (3회)
TARGETS=("50,50:24576:5" "50,50:32768:3")
MAX_TOKENS_SEQ=(1000 1500 2000 2500 3000)

OUT_DIR="${RUNTIME_DIR}/tmp/longrun-cursor-bench"
mkdir -p "${OUT_DIR}"
nvidia-smi -L > "${OUT_DIR}/gpu-map.txt"

python3 - <<'PY' > "${OUT_DIR}/results.json"
import json
print(json.dumps({"runs":[]}, ensure_ascii=False))
PY

wait_ready() {
  for _ in $(seq 1 120); do
    if curl -fsS "${BASE_URL}/models" > "${OUT_DIR}/models.json" 2>"${OUT_DIR}/models.err"; then
      return 0
    fi
    sleep 2
  done
  return 1
}

append_fail() {
  python3 - "${OUT_DIR}/results.json" "$1" "$2" "$3" "$4" <<'PY'
import json, re, sys
rp, logf, split, ctx, run_idx = sys.argv[1:]
d=json.load(open(rp, encoding="utf-8"))
txt=open(logf, encoding="utf-8", errors="ignore").read()
errs=[ln.strip() for ln in txt.splitlines() if re.search(r'out of memory|cudaMalloc failed|OOM|alloc_tensor_range|allocating .* MiB|failed to load model', ln, re.I)][-12:]
cuda=[ln.strip() for ln in txt.splitlines() if "CUDA0" in ln or "CUDA1" in ln]
d["runs"].append({
  "split": split, "context": int(ctx), "run": int(run_idx), "status": "fail",
  "oom": bool(re.search(r'out of memory|cudaMalloc failed|OOM|alloc_tensor_range', txt, re.I)),
  "error_log": errs, "cuda_map": cuda
})
json.dump(d, open(rp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
}

append_ok() {
  python3 - "${OUT_DIR}/results.json" "$1" "$2" "$3" "$4" "$5" "$6" "$7" <<'PY'
import csv, json, re, sys
rp, respf, smif, logf, split, ctx, run_idx, max_tokens = sys.argv[1:]
d=json.load(open(rp, encoding="utf-8"))
resp=json.load(open(respf, encoding="utf-8"))
tim=resp.get("timings", {})
txt=open(logf, encoding="utf-8", errors="ignore").read()
gpu={0:{"mem":0.0,"tot":0.0,"util":0.0,"power":0.0,"temp":0.0},1:{"mem":0.0,"tot":0.0,"util":0.0,"power":0.0,"temp":0.0}}
with open(smif, encoding="utf-8") as f:
  for row in csv.reader(f):
    if len(row)<6: continue
    i=int(row[0].strip())
    if i not in gpu: continue
    vals=[float(x.strip()) for x in row[1:6]]
    gpu[i]["mem"]=max(gpu[i]["mem"],vals[0]); gpu[i]["tot"]=max(gpu[i]["tot"],vals[1]); gpu[i]["util"]=max(gpu[i]["util"],vals[2]); gpu[i]["power"]=max(gpu[i]["power"],vals[3]); gpu[i]["temp"]=max(gpu[i]["temp"],vals[4])
mem_ratio=((gpu[0]["mem"]+gpu[1]["mem"])/(gpu[0]["tot"]+gpu[1]["tot"])*100.0) if (gpu[0]["tot"]+gpu[1]["tot"])>0 else 0.0
errs=[ln.strip() for ln in txt.splitlines() if re.search(r'out of memory|cudaMalloc failed|OOM|alloc_tensor_range|allocating .* MiB|failed to load model', ln, re.I)][-12:]
d["runs"].append({
  "split": split, "context": int(ctx), "run": int(run_idx), "status": "ok", "max_tokens": int(max_tokens),
  "gen_tok_s": float(tim.get("predicted_per_second", 0.0)),
  "prompt_tok_s": float(tim.get("prompt_per_second", 0.0)),
  "ttft_ms": float(tim.get("prompt_ms", 0.0)) + float(tim.get("predicted_per_token_ms", 0.0)),
  "first_response_s": float(resp.get("_elapsed_s", 0.0)),
  "completion_tokens": int(resp.get("usage",{}).get("completion_tokens",0)),
  "gpu0_vram_mib": gpu[0]["mem"], "gpu1_vram_mib": gpu[1]["mem"],
  "gpu0_power_w": gpu[0]["power"], "gpu1_power_w": gpu[1]["power"],
  "gpu0_util": gpu[0]["util"], "gpu1_util": gpu[1]["util"],
  "gpu_mem_usage_pct_total": mem_ratio, "error_log": errs
})
json.dump(d, open(rp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
}

run_case() {
  local split="$1" ctx="$2" run_idx="$3" max_tokens="$4"
  local run_id="split${split//,/}_ctx${ctx}_r${run_idx}_mt${max_tokens}"
  echo "==> ${run_id}"
  docker compose down >/dev/null 2>&1 || true
  CONTEXT_SIZE="${ctx}" SPLIT_MODE="${SPLIT_MODE}" MAIN_GPU="${MAIN_GPU}" TENSOR_SPLIT="${split}" docker compose up -d --force-recreate >/dev/null
  docker compose logs --no-color --tail=220 llama-server > "${OUT_DIR}/${run_id}.startup.log" || true
  if ! wait_ready; then
    docker compose logs --no-color --tail=260 llama-server > "${OUT_DIR}/${run_id}.startup.log" || true
    append_fail "${OUT_DIR}/${run_id}.startup.log" "${split}" "${ctx}" "${run_idx}"
    return 1
  fi
  local model_id
  model_id="$(python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print((d.get("data") or [{}])[0].get("id",""))' < "${OUT_DIR}/models.json")"
  if [[ -z "${model_id}" ]]; then
    append_fail "${OUT_DIR}/${run_id}.startup.log" "${split}" "${ctx}" "${run_idx}"
    return 1
  fi
  nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu,power.draw,temperature.gpu --format=csv,noheader,nounits -l 1 > "${OUT_DIR}/${run_id}.smi.csv" &
  local smi_pid=$!
  python3 - "${BASE_URL}" "${model_id}" "${OUT_DIR}/${run_id}.response.json" "${max_tokens}" <<'PY'
import json, sys, time, urllib.request
base_url, model_id, out_file, max_tokens = sys.argv[1:]
prompt = (
  "너는 시니어 소프트웨어 엔지니어다. 아래 요구사항을 만족하는 리팩터링 계획과 코드 변경안을 한국어로 작성하라.\n"
  "1) 모듈 경계 정리\n2) 예외 처리 강화\n3) 테스트 전략 제시\n4) 잠재 리스크와 롤백 전략 포함\n"
  "그리고 마지막에 체크리스트를 제공하라."
)
payload = {
  "model": model_id,
  "messages": [{"role":"user","content":prompt}],
  "temperature": 0.2,
  "max_tokens": int(max_tokens),
  "stream": False
}
req = urllib.request.Request(
  f"{base_url}/chat/completions",
  data=json.dumps(payload).encode("utf-8"),
  headers={"Content-Type":"application/json","Authorization":"Bearer dummy-key"},
  method="POST"
)
t0=time.perf_counter()
with urllib.request.urlopen(req, timeout=1800) as r:
  body=r.read()
t1=time.perf_counter()
d=json.loads(body.decode("utf-8"))
d["_elapsed_s"]=t1-t0
json.dump(d, open(out_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
  kill "${smi_pid}" >/dev/null 2>&1 || true
  wait "${smi_pid}" 2>/dev/null || true
  docker compose logs --no-color --tail=260 llama-server > "${OUT_DIR}/${run_id}.startup.log" || true
  append_ok "${OUT_DIR}/${run_id}.response.json" "${OUT_DIR}/${run_id}.smi.csv" "${OUT_DIR}/${run_id}.startup.log" "${split}" "${ctx}" "${run_idx}" "${max_tokens}"
}

for target in "${TARGETS[@]}"; do
  IFS=':' read -r split ctx reps <<< "${target}"
  for i in $(seq 1 "${reps}"); do
    tok_idx=$(( (i - 1) % ${#MAX_TOKENS_SEQ[@]} ))
    mt="${MAX_TOKENS_SEQ[$tok_idx]}"
    run_case "${split}" "${ctx}" "${i}" "${mt}" || true
  done
done

python3 - "${OUT_DIR}/results.json" <<'PY'
import json, sys
from collections import defaultdict

d=json.load(open(sys.argv[1], encoding="utf-8"))
grp=defaultdict(list)
for r in d.get("runs", []):
    grp[(r["split"], r["context"])].append(r)

def avg(xs): return sum(xs)/len(xs) if xs else 0.0

print("| Split | Context | Status | Gen tok/s | Prompt tok/s | TTFT | GPU0 VRAM | GPU1 VRAM | GPU0 W | GPU1 W | 비고 |")
print("|--------|---------|--------|-----------|--------------|------|-----------|-----------|--------|--------|------|")
for k in sorted(grp.keys(), key=lambda x:(x[0],x[1])):
    split, ctx = k
    items=grp[k]
    ok=[x for x in items if x["status"]=="ok"]
    fail=[x for x in items if x["status"]!="ok"]
    if ok:
      note=f"runs={len(ok)}/{len(items)}, max_tokens_avg={avg([x.get('max_tokens',0) for x in ok]):.0f}"
      if fail:
        note += ", fail 있음"
      print(f"| {split} | {ctx} | ok({len(ok)}/{len(items)}) | {avg([x['gen_tok_s'] for x in ok]):.2f} | {avg([x['prompt_tok_s'] for x in ok]):.2f} | {avg([x['ttft_ms'] for x in ok]):.2f} | {avg([x['gpu0_vram_mib'] for x in ok]):.0f} MiB | {avg([x['gpu1_vram_mib'] for x in ok]):.0f} MiB | {avg([x['gpu0_power_w'] for x in ok]):.1f} | {avg([x['gpu1_power_w'] for x in ok]):.1f} | {note} |")
    else:
      print(f"| {split} | {ctx} | fail({len(fail)}/{len(items)}) | - | - | - | - | - | - | - | {('; '.join((fail[-1].get('error_log') or ['fail'])[:2]))[:120]} |")
print("\n[raw] tmp/longrun-cursor-bench/results.json")
print("[gpu-map] tmp/longrun-cursor-bench/gpu-map.txt")
PY

