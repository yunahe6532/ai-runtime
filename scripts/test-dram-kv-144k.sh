#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

PORT="${PORT:-8080}"
BASE_URL="http://localhost:${PORT}/v1"
OUT_DIR="${RUNTIME_DIR}/tmp/dram-kv-144k"
mkdir -p "${OUT_DIR}"

# 144785 토큰 요청을 커버할 ctx (여유 포함)
CTX_SIZE="${CTX_SIZE:-147456}"
PARALLEL="${PARALLEL:-1}"
NO_KV_OFFLOAD="${NO_KV_OFFLOAD:-1}"
CACHE_TYPE_K="${CACHE_TYPE_K:-q8_0}"
CACHE_TYPE_V="${CACHE_TYPE_V:-q8_0}"
CACHE_RAM="${CACHE_RAM:-0}"

echo "=== DRAM KV 144k 테스트 ==="
echo "ctx=${CTX_SIZE}, parallel=${PARALLEL}, no-kv-offload=${NO_KV_OFFLOAD}"
echo "cache-type-k=${CACHE_TYPE_K}, cache-type-v=${CACHE_TYPE_V}, cache-ram=${CACHE_RAM}"
free -h | tee "${OUT_DIR}/mem-before.txt"

docker compose down >/dev/null 2>&1 || true
CONTEXT_SIZE="${CTX_SIZE}" \
PARALLEL="${PARALLEL}" \
NO_KV_OFFLOAD="${NO_KV_OFFLOAD}" \
CACHE_TYPE_K="${CACHE_TYPE_K}" \
CACHE_TYPE_V="${CACHE_TYPE_V}" \
CACHE_RAM="${CACHE_RAM}" \
docker compose up -d --force-recreate

echo "기동 대기 중..."
ready=0
for i in $(seq 1 180); do
  if curl -fsS "${BASE_URL}/models" > "${OUT_DIR}/models.json" 2>"${OUT_DIR}/models.err"; then
    ready=1
    break
  fi
  sleep 3
done

docker compose logs --no-color --tail=300 llama-server > "${OUT_DIR}/startup.log" || true
docker top cursor-local-llm -eo args | sed -n '2p' > "${OUT_DIR}/process.args" || true

if [[ "${ready}" -ne 1 ]]; then
  echo "FAIL: 서버 기동 실패"
  rg -i "OOM|out of memory|cudaMalloc|kv cache|failed" "${OUT_DIR}/startup.log" | tail -n 20 || true
  exit 1
fi

python3 -c 'import json; d=json.load(open("'"${OUT_DIR}"'/models.json")); print(json.dumps(d["data"][0]["meta"], ensure_ascii=False, indent=2))'

MODEL_ID="$(python3 -c 'import json; d=json.load(open("'"${OUT_DIR}"'/models.json")); print(d["data"][0]["id"])')"

echo "짧은 생성 테스트..."
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits > "${OUT_DIR}/gpu-before.smi"
free -h > "${OUT_DIR}/mem-at-ready.txt"

python3 - "${BASE_URL}" "${MODEL_ID}" "${OUT_DIR}/short.response.json" <<'PY'
import json, sys, time, urllib.request
base_url, model_id, out_file = sys.argv[1:]
payload = {
  "model": model_id,
  "messages": [{"role": "user", "content": "DRAM KV 144k 설정 확인용으로 한국어 2문장 답변"}],
  "temperature": 0.2,
  "max_tokens": 128,
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
  "content_preview": (d.get("choices", [{}])[0].get("message", {}).get("content", "")[:120]),
}
json.dump(out, open(out_file, "w", encoding="utf-8"), indent=2)
print(json.dumps(out, ensure_ascii=False, indent=2))
PY

nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits > "${OUT_DIR}/gpu-after.smi"
free -h > "${OUT_DIR}/mem-after.txt"

echo ""
echo "=== 요약 ==="
echo "process: $(cat "${OUT_DIR}/process.args")"
echo "n_ctx: $(python3 -c 'import json; print(json.load(open("'"${OUT_DIR}"'/models.json"))["data"][0]["meta"]["n_ctx"])')"
rg -n "n_parallel|n_slots|n_ctx|KV|kv cache|CPU|CUDA|offload" "${OUT_DIR}/startup.log" | tail -n 20 || true
echo "GPU mem before/after:"
cat "${OUT_DIR}/gpu-before.smi" "${OUT_DIR}/gpu-after.smi"
echo "Host mem after:"
cat "${OUT_DIR}/mem-after.txt"
