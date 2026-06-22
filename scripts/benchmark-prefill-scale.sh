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
OUT_DIR="${RUNTIME_DIR}/tmp/prefill-scale-bench"
mkdir -p "${OUT_DIR}"

TARGETS=(32768 65536 98304 131072 145000 160000 180000 190000)

echo "=== 실제 prompt prefill 스케일 테스트 (ctx=${CTX_SIZE}) ==="
free -h | tee "${OUT_DIR}/mem-before.txt"

docker compose down >/dev/null 2>&1 || true
CONTEXT_SIZE="${CTX_SIZE}" \
PARALLEL=1 \
NO_KV_OFFLOAD=1 \
CACHE_TYPE_K=q8_0 \
CACHE_TYPE_V=q8_0 \
CACHE_RAM=0 \
THREADS="${THREADS}" \
THREADS_BATCH="${THREADS_BATCH}" \
docker compose up -d --force-recreate

ready=0
for _ in $(seq 1 180); do
  if curl -fsS "${BASE_URL}/v1/models" > "${OUT_DIR}/models.json" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 3
done

docker compose logs --no-color --tail=80 llama-server > "${OUT_DIR}/startup.log" || true
if [[ "${ready}" -ne 1 ]]; then
  echo "서버 기동 실패"
  exit 1
fi

MODEL_ID="$(python3 -c 'import json; print(json.load(open("'"${OUT_DIR}"'/models.json"))["data"][0]["id"])')"
N_CTX="$(python3 -c 'import json; print(json.load(open("'"${OUT_DIR}"'/models.json"))["data"][0]["meta"]["n_ctx"])')"
echo "model=${MODEL_ID}, n_ctx=${N_CTX}"

python3 - <<'PY' > "${OUT_DIR}/results.json"
import json
print(json.dumps({"n_ctx": None, "runs": []}, ensure_ascii=False))
PY

python3 - "${BASE_URL}" "${OUT_DIR}" "${MODEL_ID}" "${TIMEOUT_SEC}" "${OUT_DIR}/results.json" <<'PY'
import json, os, sys, time, urllib.request

base_url, out_dir, model_id, timeout_sec, results_path = sys.argv[1:]
targets = [32768, 65536, 98304, 131072, 145000, 160000, 180000, 190000]
chunk = (
    "리팩터링 메모: 모듈 경계 정리, 예외 처리 강화, 테스트 전략, 롤백 전략, "
    "성능 병목 분석, API 계약 검토, 데이터 마이그레이션, 보안 점검, 로그 구조화, "
    "관측성 개선, 장애 대응 runbook, 배포 파이프라인, 캐시 정책, 동시성 제어. "
)

def post_json(url, payload, timeout=120):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer dummy-key"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def token_count(text: str) -> int:
    d = post_json(f"{base_url}/tokenize", {"content": text, "add_special": False}, timeout=300)
    return len(d.get("tokens", []))

def build_prompt(target: int) -> tuple[str, int]:
    # chunk 1회 토큰화 후 repeat 횟수만 이진 탐색 (target*3 문자열 생성 방지)
    chunk_n = token_count(chunk)
    if chunk_n <= 0:
        raise RuntimeError("chunk tokenize failed")

    cached_path = f"{out_dir}/prompt_{target}.txt"
    if target > 32768:
        # 더 작은 캐시에서 확장하면 tokenize 호출 수를 줄일 수 있다.
        for base in sorted(
            [t for t in targets if t < target],
            reverse=True,
        ):
            base_path = f"{out_dir}/prompt_{base}.txt"
            if not os.path.exists(base_path):
                continue
            base_text = open(base_path, encoding="utf-8").read()
            base_n = token_count(base_text)
            if base_n <= 0:
                continue
            text = base_text
            n = base_n
            while n < target:
                text += chunk
                n = token_count(text)
                if len(text) > target * 12:
                    break
            if n >= target * 0.995:
                open(cached_path, "w", encoding="utf-8").write(text)
                return text, n

    low, high = 1, max(2, (target // chunk_n) + 4)
    best_text, best_n = chunk, chunk_n
    while low <= high:
        mid = (low + high) // 2
        text = chunk * mid
        n = token_count(text)
        best_text, best_n = text, n
        if n < target:
            low = mid + 1
        else:
            high = mid - 1

    text = best_text
    n = best_n
    while n < target:
        text += chunk
        n = token_count(text)
    open(cached_path, "w", encoding="utf-8").write(text)
    return text, n

def read_mem_available_mib():
    with open("/proc/meminfo", encoding="utf-8") as f:
        info = {}
        for line in f:
            k, v = line.split(":", 1)
            info[k.strip()] = int(v.strip().split()[0])
    # MemAvailable kB
    return info.get("MemAvailable", 0) / 1024.0

def read_gpu_vram():
    import subprocess
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"],
        text=True,
    )
    vals = {}
    for line in out.strip().splitlines():
        i, mem = [x.strip() for x in line.split(",")]
        vals[int(i)] = float(mem)
    return vals.get(0, 0.0), vals.get(1, 0.0)

results = json.load(open(results_path, encoding="utf-8"))
results["n_ctx"] = json.load(open(f"{out_dir}/models.json", encoding="utf-8"))["data"][0]["meta"]["n_ctx"]
done = {r["target_prompt_tokens"] for r in results["runs"] if r.get("status") == "ok"}
results["runs"] = [r for r in results["runs"] if r.get("status") == "ok"]

for target in targets:
    if target in done:
        print(f"\n==> target_prompt_tokens={target} (skip: already ok)")
        continue
    print(f"\n==> target_prompt_tokens={target}")
    row = {
        "target_prompt_tokens": target,
        "status": "fail",
        "oom": False,
        "timeout": False,
        "note": "",
    }
    mem_before = read_mem_available_mib()
    try:
        prompt, actual_before = build_prompt(target)
        open(f"{out_dir}/prompt_{target}.txt", "w", encoding="utf-8").write(prompt)
        row["built_prompt_tokens"] = actual_before

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
        g0, g1 = read_gpu_vram()
        mem_after = read_mem_available_mib()
        row.update({
            "status": "ok",
            "actual_prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "prompt_tok_s": float(tim.get("prompt_per_second", 0.0)),
            "gen_tok_s": float(tim.get("predicted_per_second", 0.0)),
            "ttft_ms": float(tim.get("prompt_ms", 0.0)) + float(tim.get("predicted_per_token_ms", 0.0)),
            "prompt_ms": float(tim.get("prompt_ms", 0.0)),
            "total_sec": t1 - t0,
            "gpu0_vram_mib": g0,
            "gpu1_vram_mib": g1,
            "ram_delta_mib": mem_before - mem_after,
            "note": (d.get("choices", [{}])[0].get("message", {}).get("content", "")[:80]).replace("\n", " "),
        })
        print(json.dumps({k: row[k] for k in row if k != "note"}, ensure_ascii=False))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        row["note"] = body[:300]
        if "out of memory" in body.lower() or "oom" in body.lower():
            row["oom"] = True
        print(f"HTTPError {e.code}: {body[:200]}")
    except Exception as e:
        msg = str(e)
        row["note"] = msg[:300]
        if "timed out" in msg.lower():
            row["timeout"] = True
        if "out of memory" in msg.lower() or "oom" in msg.lower():
            row["oom"] = True
        print(f"ERROR: {msg}")
    results["runs"].append(row)
    json.dump(results, open(results_path, "w", encoding="utf-8"), indent=2)

print("\nTABLE")
print("| Target prompt | Actual prompt tokens | Status | Prompt tok/s | Gen tok/s | TTFT | Total sec | GPU0 VRAM | GPU1 VRAM | RAM delta | Note |")
print("|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---|")
for r in results["runs"]:
    if r["status"] == "ok":
        print(
            f"| {r['target_prompt_tokens']} | {r.get('actual_prompt_tokens','-')} | ok | "
            f"{r.get('prompt_tok_s',0):.2f} | {r.get('gen_tok_s',0):.2f} | {r.get('ttft_ms',0):.1f} | "
            f"{r.get('total_sec',0):.1f} | {r.get('gpu0_vram_mib',0):.0f} | {r.get('gpu1_vram_mib',0):.0f} | "
            f"{r.get('ram_delta_mib',0):.0f} | {r.get('note','')[:60]} |"
        )
    else:
        flags = []
        if r.get("oom"): flags.append("OOM")
        if r.get("timeout"): flags.append("TIMEOUT")
        note = ", ".join(flags + [r.get("note","")[:40]])
        print(
            f"| {r['target_prompt_tokens']} | {r.get('actual_prompt_tokens', r.get('built_prompt_tokens','-'))} | fail | "
            f"- | - | - | - | - | - | - | {note} |"
        )
PY

echo ""
echo "[raw] ${OUT_DIR}/results.json"
