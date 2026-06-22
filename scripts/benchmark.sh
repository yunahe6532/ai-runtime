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
CONTEXT_SIZE="${CONTEXT_SIZE:-4096}"

echo "[1/3] /v1/models 확인"
MODELS_JSON="$(curl -sS "${BASE_URL}/models")"
echo "${MODELS_JSON}"

MODEL_ID="$(python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print((d.get("data") or [{}])[0].get("id",""))' <<< "${MODELS_JSON}")"

if [[ -z "${MODEL_ID}" ]]; then
  echo "모델 ID를 찾지 못했습니다."
  exit 1
fi

echo "벤치마크 대상 모델: ${MODEL_ID}"
echo "설정된 ctx-size: ${CONTEXT_SIZE}"

echo "[2/3] chat/completions 벤치마크 시작 (model=${MODEL_ID})"

python3 - "${BASE_URL}" "${MODEL_ID}" <<'PY'
import json
import sys
import time
import urllib.request

base_url = sys.argv[1]
model_id = sys.argv[2]
url = f"{base_url}/chat/completions"

payload = {
    "model": model_id,
    "messages": [{"role": "user", "content": "한 문장으로 로컬 LLM 벤치마크 테스트 응답을 해줘."}],
    "temperature": 0.2,
    "max_tokens": 128,
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
with urllib.request.urlopen(req, timeout=300) as resp:
    body = resp.read()
t1 = time.perf_counter()

result = json.loads(body.decode("utf-8"))
usage = result.get("usage", {})
completion_tokens = usage.get("completion_tokens", 0)
elapsed = t1 - t0

ttft = result.get("timings", {}).get("predicted_per_token_ms")
if ttft is None:
    ttft = result.get("timings", {}).get("prompt_ms")
if ttft is None:
    ttft = elapsed * 1000

tps = (completion_tokens / elapsed) if elapsed > 0 and completion_tokens > 0 else 0.0

print("[3/3] 결과")
print(f"TTFT(ms): {ttft:.2f}" if isinstance(ttft, (int, float)) else f"TTFT(ms): {ttft}")
print(f"tokens/sec: {tps:.2f}")
print("응답 미리보기:", (result.get("choices", [{}])[0].get("message", {}).get("content", "")[:120]).replace("\n", " "))
PY
