#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

OUT_DIR="${RUNTIME_DIR}/tmp/coder-fast-vs-vl-bench"
mkdir -p "${OUT_DIR}"

VL_BASELINE="${RUNTIME_DIR}/tmp/longrun-cursor-bench/results.json"

wait_container_ready() {
  local container="$1"
  local port="$2"
  local retries="${3:-120}"
  for _ in $(seq 1 "${retries}"); do
    if docker exec "${container}" wget -q -O - "http://127.0.0.1:${port}/v1/models" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

bench_in_container() {
  local label="$1"
  local backend_url="$2"
  docker exec cursor-local-llm-router python3 - "${label}" "${backend_url}" <<'PY'
import json, sys, time, urllib.request
label, url = sys.argv[1], sys.argv[2]
prompt = "로컬 LLM 벤치마크를 위해 동일한 포맷으로 한국어 3문장 응답을 작성해줘."
rows = []
for mt, name in [(256, "mt256"), (1000, "mt1000")]:
    body = {"model":"model.gguf","stream":False,"max_tokens":mt,
            "messages":[{"role":"user","content":prompt}]}
    t0 = time.perf_counter()
    req = urllib.request.Request(f"{url}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type":"application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=300))
    wall = (time.perf_counter()-t0)*1000
    t = d.get("timings") or {}
    u = d.get("usage") or {}
    row = {
        "label": label, "case": name, "wall_ms": round(wall,1),
        "gen_tok_s": float(t.get("predicted_per_second") or 0),
        "prompt_tok_s": float(t.get("prompt_per_second") or 0),
        "completion_tokens": int(u.get("completion_tokens") or 0),
    }
    rows.append(row)
    print(f"  {label}/{name}: gen={row['gen_tok_s']:.1f} tok/s wall={wall:.0f}ms out={row['completion_tokens']}", file=sys.stderr)
print(json.dumps(rows))
PY
}

echo "=== [1/4] Coder fast (ctx 24k, direct llama-fast) ==="
docker compose stop llama-long llama-vl >/dev/null 2>&1 || true
docker compose up -d llama-fast router
wait_container_ready cursor-local-llm-fast 8081 || { echo "Coder fast not ready"; exit 1; }
CODER_ROWS="$(bench_in_container "coder_fast" "http://llama-fast:8081")"

echo ""
echo "=== [2/4] VL baseline (historical ctx 24k) ==="
if [[ -f "${VL_BASELINE}" ]]; then
  python3 - "${VL_BASELINE}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
runs = [r for r in data.get("runs", []) if r.get("context") == 24576 and r.get("status") == "ok"]
for r in runs[:3]:
    print(f"  vl_baseline/mt{r['max_tokens']}: gen={r['gen_tok_s']:.1f} tok/s")
PY
fi

echo ""
echo "=== [3/4] VL live text-only (llama-vl ctx 32k) ==="
docker compose stop llama-fast llama-long >/dev/null 2>&1 || true
docker compose up -d llama-vl router
if wait_container_ready cursor-local-llm-vl 8083 180; then
  VL_ROWS="$(bench_in_container "vl_live" "http://llama-vl:8083")"
else
  echo "  VL not ready — skip"
  VL_ROWS="[]"
fi

echo ""
echo "=== [4/4] Restore Coder long ==="
docker compose stop llama-vl llama-fast >/dev/null 2>&1 || true
docker compose up -d llama-long router
wait_container_ready cursor-local-llm-long 8082 180 || true

python3 - "${OUT_DIR}/results.json" "${CODER_ROWS}" "${VL_ROWS}" "${VL_BASELINE}" <<'PY'
import json, sys
out_path, coder_s, vl_s, baseline_path = sys.argv[1:5]
coder = json.loads(coder_s) if coder_s.strip() else []
vl_live = json.loads(vl_s) if vl_s.strip() else []
vl_baseline = []
try:
    data = json.load(open(baseline_path))
    vl_baseline = [r for r in data.get("runs", []) if r.get("context") == 24576 and r.get("status") == "ok"]
except Exception:
    pass
result = {"coder_fast": coder, "vl_live": vl_live, "vl_baseline_ctx24576": vl_baseline}
json.dump(result, open(out_path, "w"), ensure_ascii=False, indent=2)
print(f"Saved: {out_path}")
for c in coder:
    print(f"  Coder fast/{c['case']}: {c['gen_tok_s']:.1f} tok/s")
for v in vl_live:
    print(f"  VL live/{v['case']}: {v['gen_tok_s']:.1f} tok/s")
if vl_baseline:
    print(f"  VL baseline mt1000: {vl_baseline[0]['gen_tok_s']:.1f} tok/s")
if coder and vl_baseline:
    c1000 = next((r for r in coder if r["case"] == "mt1000"), coder[-1])
    v1000 = vl_baseline[0]
    pct = (c1000["gen_tok_s"] / v1000["gen_tok_s"] - 1) * 100
    print(f"  => Coder fast vs VL baseline mt1000: {pct:+.1f}%")
PY

echo "Done."
