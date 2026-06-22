#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

PORT="${PORT:-8080}"
BASE_URL="http://localhost:${PORT}/v1"
SPLITS=("40,60" "50,50")
CONTEXTS=("4096" "8192" "16384" "24576" "32768")
DISCOVERY_REPEATS=1
FINAL_REPEATS=3
MAIN_GPU=1
PROMPT="로컬 LLM 벤치마크를 위해 동일한 포맷으로 한국어 3문장 응답을 작성해줘."

OUT_DIR="${RUNTIME_DIR}/tmp/context-grid-bench"
mkdir -p "${OUT_DIR}"

nvidia-smi -L > "${OUT_DIR}/gpu-map.txt"

python3 - <<'PY' > "${OUT_DIR}/results.json"
import json
print(json.dumps({"runs":[]}, ensure_ascii=False))
PY

wait_ready() {
  local retries=120
  for _ in $(seq 1 "${retries}"); do
    if curl -fsS "${BASE_URL}/models" > "${OUT_DIR}/models.json" 2>"${OUT_DIR}/models.err"; then
      return 0
    fi
    sleep 2
  done
  return 1
}

append_fail_result() {
  python3 - "${OUT_DIR}/results.json" "$1" "$2" "$3" "$4" "$5" <<'PY'
import json, re, sys
rp, log_path, split, ctx, rep, phase = sys.argv[1:]
d=json.load(open(rp, encoding="utf-8"))
txt=open(log_path, encoding="utf-8", errors="ignore").read()
oom=bool(re.search(r'out of memory|cudaMalloc failed|OOM|alloc_tensor_range', txt, re.I))
err_lines=[ln.strip() for ln in txt.splitlines() if re.search(r'out of memory|cudaMalloc failed|OOM|alloc_tensor_range|allocating .* MiB|failed to load model', ln, re.I)][-12:]
cuda_map=[ln.strip() for ln in txt.splitlines() if "CUDA0" in ln or "CUDA1" in ln]
d["runs"].append({
  "phase": phase, "split": split, "context": int(ctx), "repeat": int(rep),
  "status": "fail", "model_load_success": False, "oom": oom,
  "error_log": err_lines or ["startup failed"], "cuda_map": cuda_map
})
json.dump(d, open(rp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
}

append_ok_result() {
  python3 - "${OUT_DIR}/results.json" "$1" "$2" "$3" "$4" "$5" "$6" "$7" <<'PY'
import csv, json, re, sys
rp, resp_path, smi_path, log_path, split, ctx, rep, phase = sys.argv[1:]
d=json.load(open(rp, encoding="utf-8"))
resp=json.load(open(resp_path, encoding="utf-8"))
tim=resp.get("timings", {})
txt=open(log_path, encoding="utf-8", errors="ignore").read()
oom=bool(re.search(r'out of memory|cudaMalloc failed|OOM|alloc_tensor_range', txt, re.I))
err_lines=[ln.strip() for ln in txt.splitlines() if re.search(r'out of memory|cudaMalloc failed|OOM|alloc_tensor_range|allocating .* MiB|failed to load model', ln, re.I)][-12:]
cuda_map=[ln.strip() for ln in txt.splitlines() if "CUDA0" in ln or "CUDA1" in ln]
gpu={0:{"mem_used":0.0,"mem_total":0.0,"util":0.0,"power":0.0,"temp":0.0},1:{"mem_used":0.0,"mem_total":0.0,"util":0.0,"power":0.0,"temp":0.0}}
with open(smi_path, encoding="utf-8") as f:
  for row in csv.reader(f):
    if len(row) < 6: continue
    i=int(row[0].strip())
    if i not in gpu: continue
    vals=[float(x.strip()) for x in row[1:6]]
    gpu[i]["mem_used"]=max(gpu[i]["mem_used"], vals[0]); gpu[i]["mem_total"]=max(gpu[i]["mem_total"], vals[1]); gpu[i]["util"]=max(gpu[i]["util"], vals[2]); gpu[i]["power"]=max(gpu[i]["power"], vals[3]); gpu[i]["temp"]=max(gpu[i]["temp"], vals[4])
mem_ratio=((gpu[0]["mem_used"]+gpu[1]["mem_used"])/(gpu[0]["mem_total"]+gpu[1]["mem_total"])*100.0) if (gpu[0]["mem_total"]+gpu[1]["mem_total"])>0 else 0.0
ttft=float(tim.get("prompt_ms",0.0))+float(tim.get("predicted_per_token_ms",0.0))
d["runs"].append({
  "phase": phase, "split": split, "context": int(ctx), "repeat": int(rep),
  "status": "ok", "model_load_success": True, "oom": oom,
  "gen_tok_s": float(tim.get("predicted_per_second", 0.0)),
  "prompt_tok_s": float(tim.get("prompt_per_second", 0.0)),
  "ttft_ms": ttft, "first_response_s": float(resp.get("_elapsed_s",0.0)),
  "gpu0_vram_mib": gpu[0]["mem_used"], "gpu1_vram_mib": gpu[1]["mem_used"],
  "gpu0_power_w": gpu[0]["power"], "gpu1_power_w": gpu[1]["power"],
  "gpu0_util": gpu[0]["util"], "gpu1_util": gpu[1]["util"],
  "gpu_mem_usage_pct_total": mem_ratio, "error_log": err_lines, "cuda_map": cuda_map
})
json.dump(d, open(rp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
}

run_once() {
  local split="$1" ctx="$2" rep="$3" phase="$4"
  local run_id="${phase}_split${split//,/}_ctx${ctx}_r${rep}"
  echo "==> ${run_id}"
  docker compose down >/dev/null 2>&1 || true
  CONTEXT_SIZE="${ctx}" SPLIT_MODE="layer" MAIN_GPU="${MAIN_GPU}" TENSOR_SPLIT="${split}" docker compose up -d --force-recreate >/dev/null
  docker compose logs --no-color --tail=220 llama-server > "${OUT_DIR}/${run_id}.startup.log" || true
  if ! wait_ready; then
    docker compose logs --no-color --tail=260 llama-server > "${OUT_DIR}/${run_id}.startup.log" || true
    append_fail_result "${OUT_DIR}/${run_id}.startup.log" "${split}" "${ctx}" "${rep}" "${phase}"
    return 1
  fi
  local model_id
  model_id="$(python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print((d.get("data") or [{}])[0].get("id",""))' < "${OUT_DIR}/models.json")"
  if [[ -z "${model_id}" ]]; then
    append_fail_result "${OUT_DIR}/${run_id}.startup.log" "${split}" "${ctx}" "${rep}" "${phase}"
    return 1
  fi
  nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu,power.draw,temperature.gpu --format=csv,noheader,nounits -l 1 > "${OUT_DIR}/${run_id}.smi.csv" &
  local smi_pid=$!
  python3 - "${BASE_URL}" "${model_id}" "${OUT_DIR}/${run_id}.response.json" "${PROMPT}" <<'PY'
import json, sys, time, urllib.request
base_url, model_id, out_file, prompt = sys.argv[1:]
url = f"{base_url}/chat/completions"
payload = {"model": model_id, "messages": [{"role":"user","content":prompt}], "temperature":0.2, "max_tokens":220, "stream":False}
req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type":"application/json","Authorization":"Bearer dummy-key"}, method="POST")
t0=time.perf_counter()
with urllib.request.urlopen(req, timeout=900) as r:
  body=r.read()
t1=time.perf_counter()
d=json.loads(body.decode("utf-8")); d["_elapsed_s"]=t1-t0
json.dump(d, open(out_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
  kill "${smi_pid}" >/dev/null 2>&1 || true
  wait "${smi_pid}" 2>/dev/null || true
  docker compose logs --no-color --tail=260 llama-server > "${OUT_DIR}/${run_id}.startup.log" || true
  append_ok_result "${OUT_DIR}/${run_id}.response.json" "${OUT_DIR}/${run_id}.smi.csv" "${OUT_DIR}/${run_id}.startup.log" "${split}" "${ctx}" "${rep}" "${phase}"
  return 0
}

declare -A MAX_SUCCESS
declare -A LOWER_SUCCESS
for split in "${SPLITS[@]}"; do
  prev=""
  for ctx in "${CONTEXTS[@]}"; do
    if run_once "${split}" "${ctx}" "1" "discovery"; then
      LOWER_SUCCESS["${split}"]="${prev}"
      MAX_SUCCESS["${split}"]="${ctx}"
      prev="${ctx}"
    else
      echo "discovery stop: split=${split}, ctx=${ctx}"
      break
    fi
  done
done

for split in "${SPLITS[@]}"; do
  for ctx in "${LOWER_SUCCESS[$split]:-}" "${MAX_SUCCESS[$split]:-}"; do
    [[ -z "${ctx}" ]] && continue
    for rep in $(seq 1 "${FINAL_REPEATS}"); do
      run_once "${split}" "${ctx}" "${rep}" "final" || true
    done
  done
done

python3 - "${OUT_DIR}/results.json" <<'PY'
import json, sys
from collections import defaultdict

d=json.load(open(sys.argv[1], encoding="utf-8"))
runs=d.get("runs",[])
grp=defaultdict(list)
for r in runs:
  grp[(r["split"], r["context"])].append(r)

print("| Split | Context | Status | Gen tok/s | Prompt tok/s | TTFT | GPU0 VRAM | GPU1 VRAM | GPU0 W | GPU1 W | 비고 |")
print("|--------|---------|--------|-----------|--------------|------|-----------|-----------|--------|--------|------|")

def avg(vals): return sum(vals)/len(vals) if vals else 0.0

for key in sorted(grp.keys(), key=lambda x:(x[0], x[1])):
  split, ctx = key
  items=grp[key]
  finals=[x for x in items if x.get("phase")=="final"]
  base=finals if finals else items
  oks=[x for x in base if x.get("status")=="ok"]
  fails=[x for x in base if x.get("status")!="ok"]
  if oks:
    status=f"ok({len(oks)}/{len(base)})"
    note="final avg" if finals else "discovery only"
    if fails:
      status=f"partial({len(oks)}/{len(base)})"
      note="; ".join((fails[-1].get("error_log") or ["fail"])[:2])[:120]
    print(f"| {split} | {ctx} | {status} | {avg([x['gen_tok_s'] for x in oks]):.2f} | {avg([x['prompt_tok_s'] for x in oks]):.2f} | {avg([x['ttft_ms'] for x in oks]):.2f} | {avg([x['gpu0_vram_mib'] for x in oks]):.0f} MiB | {avg([x['gpu1_vram_mib'] for x in oks]):.0f} MiB | {avg([x['gpu0_power_w'] for x in oks]):.1f} | {avg([x['gpu1_power_w'] for x in oks]):.1f} | {note} |")
  else:
    err=fails[-1].get("error_log") or ["fail"]
    print(f"| {split} | {ctx} | fail({len(base)}/{len(base)}) | - | - | - | - | - | - | - | {'; '.join(err[:2])[:120]} |")

print("\n[raw] tmp/context-grid-bench/results.json")
print("[gpu-map] tmp/context-grid-bench/gpu-map.txt")
PY

