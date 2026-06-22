#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"
OUT_DIR="${RUNTIME_DIR}/tmp/prefill-scale-bench"
LOG="${OUT_DIR}/resume-after-wsl.log"
mkdir -p "${OUT_DIR}"

exec > >(tee -a "${LOG}") 2>&1

echo "=== $(date -Is) WSL 재기동 후 prefill 벤치 재개 ==="
bash scripts/verify-wsl-resources.sh || true
echo ""

# Docker 기동 대기
for _ in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

docker compose down >/dev/null 2>&1 || true
CONTEXT_SIZE=200000 \
PARALLEL=1 \
NO_KV_OFFLOAD=1 \
CACHE_TYPE_K=q8_0 \
CACHE_TYPE_V=q8_0 \
CACHE_RAM=0 \
THREADS=16 \
THREADS_BATCH=16 \
docker compose up -d --force-recreate

BASE_URL="http://localhost:8080"
ready=0
for _ in $(seq 1 180); do
  if curl -fsS "${BASE_URL}/v1/models" > "${OUT_DIR}/models.json" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 3
done
if [[ "${ready}" -ne 1 ]]; then
  echo "서버 기동 실패"
  exit 1
fi

MODEL_ID="$(python3 -c 'import json; print(json.load(open("'"${OUT_DIR}"'/models.json"))["data"][0]["id"])')"
docker compose logs --no-color --tail=30 llama-server | rg -i 'n_threads|thread' || true

python3 - "${BASE_URL}" "${OUT_DIR}" "${MODEL_ID}" "${OUT_DIR}/results.json" <<'PY'
import json, os, sys, time, urllib.request, urllib.error, subprocess

base_url, out_dir, model_id, results_path = sys.argv[1:]
targets = [160000, 180000, 190000]
timeout_sec = 1800
chunk = (
    "리팩터링 메모: 모듈 경계 정리, 예외 처리 강화, 테스트 전략, 롤백 전략, "
    "성능 병목 분석, API 계약 검토, 데이터 마이그레이션, 보안 점검, 로그 구조화, "
    "관측성 개선, 장애 대응 runbook, 배포 파이프라인, 캐시 정책, 동시성 제어. "
)

def post_json(url, payload, timeout=120):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer dummy"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def token_count(text):
    return len(post_json(f"{base_url}/tokenize", {"content": text, "add_special": False}, timeout=300).get("tokens", []))

def build_prompt(target):
    p = f"{out_dir}/prompt_{target}.txt"
    if os.path.exists(p):
        text = open(p, encoding="utf-8").read()
        return text, token_count(text)
    for base in sorted([145000, 131072, 98304, 65536, 32768], reverse=True):
        bp = f"{out_dir}/prompt_{base}.txt"
        if not os.path.exists(bp):
            continue
        text = open(bp, encoding="utf-8").read()
        n = token_count(text)
        while n < target:
            text += chunk
            n = token_count(text)
        open(p, "w", encoding="utf-8").write(text)
        return text, n
    raise RuntimeError(f"no base prompt for {target}")

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

results = json.load(open(results_path, encoding="utf-8"))
done = {r["target_prompt_tokens"] for r in results["runs"] if r.get("status") == "ok"}

for target in targets:
    if target in done:
        print(f"\n==> target={target} skip (already ok)")
        continue
    print(f"\n==> target={target}")
    row = {"target_prompt_tokens": target, "status": "fail", "oom": False, "timeout": False, "note": "", "wsl_after_reboot": True}
    mem_before = read_mem()
    try:
        t_build = time.perf_counter()
        prompt, built = build_prompt(target)
        row["built_prompt_tokens"] = built
        row["build_sec"] = time.perf_counter() - t_build
        print(f"built {built} tokens in {row['build_sec']:.1f}s")
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 128,
            "stream": False,
        }
        t0 = time.perf_counter()
        d = post_json(f"{base_url}/v1/chat/completions", payload, timeout=timeout_sec)
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
            "ttft_ms": float(tim.get("prompt_ms", 0.0)) + float(tim.get("predicted_per_token_ms", 0.0)),
            "prompt_ms": float(tim.get("prompt_ms", 0.0)),
            "total_sec": t1 - t0,
            "gpu0_vram_mib": g0,
            "gpu1_vram_mib": g1,
            "ram_delta_mib": mem_before - mem_after,
        })
        print(json.dumps({k: v for k, v in row.items()}, ensure_ascii=False))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        row["note"] = body[:300]
        if "out of memory" in body.lower():
            row["oom"] = True
        print(f"HTTP {e.code}: {body[:200]}")
    except Exception as e:
        msg = str(e)
        row["note"] = msg[:300]
        if "timed out" in msg.lower():
            row["timeout"] = True
        if "out of memory" in msg.lower():
            row["oom"] = True
        print(f"ERROR: {msg}")
    results["runs"] = [r for r in results["runs"] if r.get("target_prompt_tokens") != target]
    results["runs"].append(row)
    json.dump(results, open(results_path, "w", encoding="utf-8"), indent=2)

print("\n=== FINAL TABLE ===")
print("| Target | Actual | Status | Prompt tok/s | Gen tok/s | TTFT ms | Total sec | GPU0 | GPU1 | RAM delta |")
for r in sorted(results["runs"], key=lambda x: x["target_prompt_tokens"]):
    if r["status"] == "ok":
        print(
            f"| {r['target_prompt_tokens']} | {r.get('actual_prompt_tokens')} | ok | "
            f"{r.get('prompt_tok_s', 0):.1f} | {r.get('gen_tok_s', 0):.2f} | {r.get('ttft_ms', 0):.0f} | "
            f"{r.get('total_sec', 0):.1f} | {r.get('gpu0_vram_mib', 0):.0f} | {r.get('gpu1_vram_mib', 0):.0f} | "
            f"{r.get('ram_delta_mib', 0):.0f} |"
        )
    else:
        print(f"| {r['target_prompt_tokens']} | - | fail | - | - | - | - | - | - | - | {r.get('note', '')[:40]} |")
PY

echo "=== $(date -Is) 완료 ==="
