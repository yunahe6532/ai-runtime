#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

PORT="${PORT:-8080}"
BASE_URL="http://localhost:${PORT}"
CTX_SIZE="${CTX_SIZE:-200000}"
TIMEOUT_SEC="${TIMEOUT_SEC:-1800}"
THREADS="${THREADS:-16}"
THREADS_BATCH="${THREADS_BATCH:-16}"
PROMPT_DIR="${RUNTIME_DIR}/tmp/prefill-scale-bench"
OUT_DIR="${RUNTIME_DIR}/tmp/kv-profile-bench"
mkdir -p "${OUT_DIR}"

# hybrid-kv: GPU auto-fit + DRAM spill (tensor-split 없음)
# 비교 기준: prefill-scale-bench (dram-kv 50/50 + NO_KV_OFFLOAD=1) 결과 사용
PROFILES=(
  "hybrid-kv|0||GPU auto-fit + DRAM spill (kv-offload 기본)"
)

TARGETS=(32768 145000 190000)

echo "=== KV 프로필 비교 (ctx=${CTX_SIZE}) ==="
free -h | tee "${OUT_DIR}/mem-before.txt"
echo "[]" > "${OUT_DIR}/results.json"

run_profile() {
  local name="$1"
  local no_kv_offload="$2"
  local tensor_split="$3"
  local desc="$4"
  local profile_dir="${OUT_DIR}/${name}"
  mkdir -p "${profile_dir}"

  echo ""
  echo "========== profile=${name}: ${desc} =========="

  docker compose down >/dev/null 2>&1 || true

  local env_args=(
    "CONTEXT_SIZE=${CTX_SIZE}"
    "PARALLEL=1"
    "CACHE_TYPE_K=q8_0"
    "CACHE_TYPE_V=q8_0"
    "CACHE_RAM=0"
    "THREADS=${THREADS}"
    "THREADS_BATCH=${THREADS_BATCH}"
    "MAIN_GPU=1"
  )
  if [[ -n "${no_kv_offload}" ]]; then
    env_args+=("NO_KV_OFFLOAD=${no_kv_offload}")
  fi
  if [[ -n "${tensor_split}" ]]; then
    env_args+=("TENSOR_SPLIT=${tensor_split}")
  else
    env_args+=("TENSOR_SPLIT=")
  fi

  env "${env_args[@]}" docker compose up -d --force-recreate
  sleep 3

  local ready=0
  local startup_status="fail"
  for _ in $(seq 1 180); do
    if curl -fsS "${BASE_URL}/v1/models" > "${profile_dir}/models.json" 2>/dev/null; then
      ready=1
      startup_status="ok"
      break
    fi
    sleep 3
  done

  docker compose logs --no-color llama-server > "${profile_dir}/startup.log" 2>&1 || true
  nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader > "${profile_dir}/vram-idle.txt" 2>/dev/null || true

  if [[ "${ready}" -ne 1 ]]; then
    echo "[${name}] 서버 기동 실패"
    rg -i 'oom|fail|error' "${profile_dir}/startup.log" | tail -5 || true
    python3 - <<PY
import json
out = json.load(open("${OUT_DIR}/results.json"))
out.append({
  "profile": "${name}",
  "description": """${desc}""",
  "startup": "fail",
  "no_kv_offload": "${no_kv_offload}",
  "tensor_split": "${tensor_split}",
  "runs": []
})
json.dump(out, open("${OUT_DIR}/results.json", "w"), indent=2, ensure_ascii=False)
PY
    return 0
  fi

  local model_id n_ctx
  model_id="$(python3 -c 'import json; print(json.load(open("'"${profile_dir}"'/models.json"))["data"][0]["id"])')"
  n_ctx="$(python3 -c 'import json; print(json.load(open("'"${profile_dir}"'/models.json"))["data"][0]["meta"]["n_ctx"])')"
  echo "[${name}] model=${model_id}, n_ctx=${n_ctx}"

  python3 - "${BASE_URL}" "${PROMPT_DIR}" "${profile_dir}" "${model_id}" "${TIMEOUT_SEC}" "${name}" "${desc}" "${no_kv_offload}" "${tensor_split}" "${OUT_DIR}/results.json" <<'PY'
import json, os, sys, time, urllib.request, urllib.error, subprocess

base_url, prompt_dir, profile_dir, model_id, timeout_sec, profile, desc, no_kv, ts, results_path = sys.argv[1:]
targets = [32768, 145000, 190000]
runs = []

def post_json(url, payload, timeout=120):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer dummy"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def read_mem():
    with open("/proc/meminfo", encoding="utf-8") as f:
        info = {k.strip(): int(v.split()[0]) for k, v in (line.split(":", 1) for line in f)}
    return info.get("MemAvailable", 0) / 1024.0

def read_gpu():
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"], text=True
    )
    vals = {}
    for line in out.strip().splitlines():
        i, m = [x.strip() for x in line.split(",")]
        vals[int(i)] = float(m)
    return vals.get(0, 0.0), vals.get(1, 0.0)

for target in targets:
    prompt_path = f"{prompt_dir}/prompt_{target}.txt"
    row = {"target_prompt_tokens": target, "status": "fail", "oom": False, "timeout": False}
    if not os.path.exists(prompt_path):
        row["note"] = "prompt file missing"
        runs.append(row)
        print(f"[{profile}] target={target} SKIP (no prompt file)")
        continue

    prompt = open(prompt_path, encoding="utf-8").read()
    mem_before = read_mem()
    print(f"[{profile}] target={target} running...", flush=True)
    try:
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 128,
            "stream": False,
        }
        t0 = time.perf_counter()
        d = post_json(f"{base_url}/v1/chat/completions", payload, timeout=int(timeout_sec))
        t1 = time.perf_counter()
        tim = d.get("timings", {})
        usage = d.get("usage", {})
        g0, g1 = read_gpu()
        mem_after = read_mem()
        row.update({
            "status": "ok",
            "actual_prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "prompt_tok_s": float(tim.get("prompt_per_second", 0.0)),
            "gen_tok_s": float(tim.get("predicted_per_second", 0.0)),
            "prompt_ms": float(tim.get("prompt_ms", 0.0)),
            "ttft_ms": float(tim.get("prompt_ms", 0.0)) + float(tim.get("predicted_per_token_ms", 0.0)),
            "total_sec": t1 - t0,
            "gpu0_vram_mib": g0,
            "gpu1_vram_mib": g1,
            "ram_delta_mib": mem_before - mem_after,
        })
        print(
            f"  ok actual={row['actual_prompt_tokens']} prefill={row['prompt_ms']/1000:.1f}s "
            f"prompt_tok/s={row['prompt_tok_s']:.1f} total={row['total_sec']:.1f}s",
            flush=True,
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        row["note"] = body[:300]
        if "out of memory" in body.lower():
            row["oom"] = True
        print(f"  HTTP {e.code}: {body[:120]}", flush=True)
    except Exception as e:
        msg = str(e)
        row["note"] = msg[:300]
        if "timed out" in msg.lower():
            row["timeout"] = True
        if "out of memory" in msg.lower():
            row["oom"] = True
        print(f"  ERROR: {msg}", flush=True)
    runs.append(row)

out = json.load(open(results_path, encoding="utf-8"))
out.append({
    "profile": profile,
    "description": desc,
    "startup": "ok",
    "no_kv_offload": no_kv,
    "tensor_split": ts or "auto",
    "n_ctx": json.load(open(f"{profile_dir}/models.json"))["data"][0]["meta"]["n_ctx"],
    "runs": runs,
})
json.dump(out, open(results_path, "w", encoding="utf-8"), indent=2)
PY
}

for entry in "${PROFILES[@]}"; do
  IFS='|' read -r name no_kv ts desc <<< "${entry}"
  run_profile "${name}" "${no_kv}" "${ts}" "${desc}"
done

python3 - "${OUT_DIR}/results.json" <<'PY'
import json, sys
out = json.load(open(sys.argv[1]))
print("\n=== KV 프로필 비교 요약 ===")
print("| Profile | Startup | Target | Actual | Status | Prompt tok/s | Prefill sec | Total sec | GPU0 | GPU1 | RAM Δ |")
print("|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|")
for p in out:
    if p.get("startup") != "ok":
        print(f"| {p['profile']} | fail | - | - | - | - | - | - | - | - | - |")
        continue
    for r in p.get("runs", []):
        if r.get("status") == "ok":
            print(
                f"| {p['profile']} | ok | {r['target_prompt_tokens']} | {r.get('actual_prompt_tokens')} | ok | "
                f"{r.get('prompt_tok_s', 0):.1f} | {r.get('prompt_ms', 0)/1000:.1f} | {r.get('total_sec', 0):.1f} | "
                f"{r.get('gpu0_vram_mib', 0):.0f} | {r.get('gpu1_vram_mib', 0):.0f} | {r.get('ram_delta_mib', 0):.0f} |"
            )
        else:
            note = r.get("note", "")[:30]
            print(
                f"| {p['profile']} | ok | {r['target_prompt_tokens']} | - | fail | - | - | - | - | - | - | {note} |"
            )
PY

echo ""
echo "[raw] ${OUT_DIR}/results.json"
