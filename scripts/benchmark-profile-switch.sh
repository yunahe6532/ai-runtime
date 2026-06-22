#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="/home/yunahe/ai-runtime/cursor-local-llm"
cd "${RUNTIME_DIR}"

OUT_DIR="${RUNTIME_DIR}/tmp/profile-switch-bench"
mkdir -p "${OUT_DIR}"

if [[ ! -f ".env" ]]; then
  echo ".env 없음"
  exit 1
fi

# shellcheck disable=SC1091
source ".env"

MODEL_FILE_EXPANDED="${MODEL_FILE/#\~/$HOME}"
MMPROJ_FILE_EXPANDED="${MMPROJ_FILE:-}"
MMPROJ_FILE_EXPANDED="${MMPROJ_FILE_EXPANDED/#\~/$HOME}"
export MODEL_FILE="${MODEL_FILE_EXPANDED}"
export MMPROJ_FILE="${MMPROJ_FILE_EXPANDED}"
export GPU_LAYERS="${GPU_LAYERS:--1}"

PORT="${PORT:-8080}"
BASE_URL="http://localhost:${PORT}"
READY_TIMEOUT_SEC="${READY_TIMEOUT_SEC:-600}"
CHAT_TIMEOUT_SEC="${CHAT_TIMEOUT_SEC:-120}"
VRAM_POLL_SEC="${VRAM_POLL_SEC:-60}"

echo "=== 프로필 전환 벤치 (fast GPU KV vs long DRAM KV) ==="
free -h | tee "${OUT_DIR}/mem-before.txt"

python3 - "${BASE_URL}" "${OUT_DIR}" "${READY_TIMEOUT_SEC}" "${CHAT_TIMEOUT_SEC}" "${VRAM_POLL_SEC}" <<'PY'
import json, os, subprocess, sys, time, urllib.error, urllib.request

base_url, out_dir, ready_timeout_s, chat_timeout_s, vram_poll_s = sys.argv[1:]
ready_timeout_s = int(ready_timeout_s)
chat_timeout_s = int(chat_timeout_s)
vram_poll_s = int(vram_poll_s)
runtime_dir = os.path.dirname(os.path.dirname(out_dir))

results = {
    "fast_profile": {
        "CONTEXT_SIZE": 24576,
        "TENSOR_SPLIT": "50,50",
        "MAIN_GPU": 1,
        "SPLIT_MODE": "layer",
        "PARALLEL": 1,
        "kv": "gpu-default",
    },
    "long_profile": {
        "CONTEXT_SIZE": 200000,
        "PARALLEL": 1,
        "NO_KV_OFFLOAD": 1,
        "CACHE_TYPE_K": "q8_0",
        "CACHE_TYPE_V": "q8_0",
        "CACHE_RAM": 0,
        "THREADS": 16,
        "THREADS_BATCH": 16,
        "TENSOR_SPLIT": "50,50",
        "MAIN_GPU": 1,
        "SPLIT_MODE": "layer",
    },
    "cold_start": {},
    "switches": [],
    "vram_release": [],
    "summary": {},
}

def save():
    json.dump(results, open(f"{out_dir}/results.json", "w", encoding="utf-8"), indent=2)


def run(cmd, cwd=runtime_dir, check=True):
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=check)


def read_gpu_vram():
    out = run(["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"]).stdout
    vals = {}
    for line in out.strip().splitlines():
        i, m = [x.strip() for x in line.split(",")]
        vals[int(i)] = float(m)
    return vals.get(0, 0.0), vals.get(1, 0.0)


def read_ram_used_mib():
    with open("/proc/meminfo", encoding="utf-8") as f:
        info = {k.strip(): int(v.split()[0]) for k, v in (line.split(":", 1) for line in f)}
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    return (total - avail) / 1024.0


def wait_models(timeout_s):
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        try:
            req = urllib.request.Request(f"{base_url}/v1/models", method="GET")
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status == 200:
                    return json.loads(r.read().decode("utf-8"))
        except Exception:
            pass
        time.sleep(1)
    return None


def short_chat(model_id, timeout_s):
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16,
        "temperature": 0.2,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer dummy"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return json.loads(r.read().decode("utf-8"))


def compose_down():
    t0 = time.perf_counter()
    run(["docker", "compose", "down"], check=False)
    return time.perf_counter() - t0


def profile_env(profile: str) -> dict:
    common = {
        "MODEL_FILE": os.environ["MODEL_FILE"],
        "MMPROJ_FILE": os.environ.get("MMPROJ_FILE", ""),
        "GPU_LAYERS": os.environ.get("GPU_LAYERS", "-1"),
        "PORT": os.environ.get("PORT", "8080"),
        "SPLIT_MODE": "layer",
        "MAIN_GPU": "1",
        "TENSOR_SPLIT": "50,50",
        "PARALLEL": "1",
        "NO_KV_OFFLOAD": "",
        "CACHE_TYPE_K": "",
        "CACHE_TYPE_V": "",
        "CACHE_RAM": "",
        "THREADS": "",
        "THREADS_BATCH": "",
    }
    if profile == "fast":
        common["CONTEXT_SIZE"] = "24576"
    elif profile == "long":
        common.update({
            "CONTEXT_SIZE": "200000",
            "NO_KV_OFFLOAD": "1",
            "CACHE_TYPE_K": "q8_0",
            "CACHE_TYPE_V": "q8_0",
            "CACHE_RAM": "0",
            "THREADS": "16",
            "THREADS_BATCH": "16",
        })
    else:
        raise ValueError(profile)
    return common


def compose_up(profile: str):
    env = os.environ.copy()
    env.update(profile_env(profile))
    t0 = time.perf_counter()
    p = subprocess.run(
        ["docker", "compose", "up", "-d", "--force-recreate"],
        cwd=runtime_dir,
        env=env,
        text=True,
        capture_output=True,
    )
    up_sec = time.perf_counter() - t0
    if p.returncode != 0:
        raise RuntimeError(f"compose up failed ({profile}): {p.stderr[-500:]}")
    return up_sec


def capture_logs(tag):
    p = run(["docker", "compose", "logs", "--no-color", "--tail=120", "llama-server"], check=False)
    open(f"{out_dir}/{tag}.startup.log", "w", encoding="utf-8").write(p.stdout)


def detect_oom(log_path: str) -> bool:
    if not os.path.exists(log_path):
        return False
    text = open(log_path, encoding="utf-8", errors="ignore").read().lower()
    return "out of memory" in text or "oom" in text or "cudamalloc failed" in text


def measure_vram_release(after_profile: str):
    g0_before, g1_before = read_gpu_vram()
    entry = {
        "after_profile": after_profile,
        "vram_at_down_gpu0": g0_before,
        "vram_at_down_gpu1": g1_before,
        "samples": [],
        "released_gpu0_sec": None,
        "released_gpu1_sec": None,
        "idle_gpu0_mib": None,
        "idle_gpu1_mib": None,
    }
    compose_down()
    threshold = 500.0  # MiB 이하를 idle로 간주
    t0 = time.perf_counter()
    for _ in range(vram_poll_s):
        g0, g1 = read_gpu_vram()
        elapsed = time.perf_counter() - t0
        entry["samples"].append({"sec": round(elapsed, 1), "gpu0": g0, "gpu1": g1})
        if entry["released_gpu0_sec"] is None and g0 <= threshold:
            entry["released_gpu0_sec"] = round(elapsed, 1)
        if entry["released_gpu1_sec"] is None and g1 <= threshold:
            entry["released_gpu1_sec"] = round(elapsed, 1)
        if g0 <= threshold and g1 <= threshold:
            entry["idle_gpu0_mib"] = g0
            entry["idle_gpu1_mib"] = g1
            break
        time.sleep(1)
    else:
        g0, g1 = read_gpu_vram()
        entry["idle_gpu0_mib"] = g0
        entry["idle_gpu1_mib"] = g1
    results["vram_release"].append(entry)
    save()
    return entry


def bring_up_and_measure(direction: str, run_no: int, profile: str, kind: str):
  # kind: cold_start | switch
    tag = f"{kind}_{direction.replace('->', '_to_')}_r{run_no}"
    row = {
        "kind": kind,
        "direction": direction,
        "run": run_no,
        "target_profile": profile,
        "status": "fail",
        "down_sec": None,
        "up_sec": None,
        "ready_sec": None,
        "first_response_sec": None,
        "total_sec": None,
        "gpu0_vram_mib": None,
        "gpu1_vram_mib": None,
        "ram_used_mib": None,
        "oom": False,
        "note": "",
    }

    total_t0 = time.perf_counter()
    try:
        row["down_sec"] = round(compose_down(), 2)
        row["up_sec"] = round(compose_up(profile), 2)
        up_t0 = time.perf_counter()

        models = wait_models(ready_timeout_s)
        if models is None:
            row["note"] = "models timeout"
            capture_logs(tag)
            row["oom"] = detect_oom(f"{out_dir}/{tag}.startup.log")
            results["switches"].append(row)
            save()
            return row

        row["ready_sec"] = round(time.perf_counter() - up_t0, 2)
        model_id = models["data"][0]["id"]

        chat_t0 = time.perf_counter()
        short_chat(model_id, chat_timeout_s)
        row["first_response_sec"] = round(time.perf_counter() - up_t0, 2)
        row["first_chat_after_ready_sec"] = round(time.perf_counter() - chat_t0, 2)
        row["total_sec"] = round(time.perf_counter() - total_t0, 2)

        g0, g1 = read_gpu_vram()
        row["gpu0_vram_mib"] = g0
        row["gpu1_vram_mib"] = g1
        row["ram_used_mib"] = round(read_ram_used_mib(), 1)
        row["status"] = "ok"
        capture_logs(tag)
        row["oom"] = detect_oom(f"{out_dir}/{tag}.startup.log")
    except Exception as e:
        row["note"] = str(e)[:300]
        capture_logs(tag)
        row["oom"] = detect_oom(f"{out_dir}/{tag}.startup.log")
        run(["docker", "compose", "logs", "--no-color", "--tail=80", "llama-server"], check=False)

    if kind == "cold_start":
        results["cold_start"][profile] = row
    else:
        results["switches"].append(row)
    save()
    print(json.dumps({k: row[k] for k in row if k != "note"}, ensure_ascii=False), flush=True)
    if row.get("note"):
        print(f"  note: {row['note']}", flush=True)
    return row


# --- cold start: fast ---
print("\n== cold start: fast", flush=True)
compose_down()
measure_vram_release("idle")
bring_up_and_measure("cold", 1, "fast", "cold_start")
g0, g1 = read_gpu_vram()
results["cold_start"]["fast"]["vram_while_running_gpu0"] = g0
results["cold_start"]["fast"]["vram_while_running_gpu1"] = g1
save()

print("\n== VRAM release after fast", flush=True)
measure_vram_release("fast")

# --- cold start: long ---
print("\n== cold start: long", flush=True)
bring_up_and_measure("cold", 1, "long", "cold_start")
g0, g1 = read_gpu_vram()
results["cold_start"]["long"]["vram_while_running_gpu0"] = g0
results["cold_start"]["long"]["vram_while_running_gpu1"] = g1
save()

print("\n== VRAM release after long", flush=True)
measure_vram_release("long")

# --- switch fast -> long x3 ---
for i in range(1, 4):
    print(f"\n== switch fast->long run {i}", flush=True)
    # ensure fast is up first
    compose_down()
    compose_up("fast")
    if wait_models(ready_timeout_s) is None:
        print("  warn: fast warmup failed", flush=True)
    bring_up_and_measure("fast->long", i, "long", "switch")

# --- switch long -> fast x3 ---
for i in range(1, 4):
    print(f"\n== switch long->fast run {i}", flush=True)
    compose_down()
    compose_up("long")
    if wait_models(ready_timeout_s) is None:
        print("  warn: long warmup failed", flush=True)
    bring_up_and_measure("long->fast", i, "fast", "switch")


def stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"min": None, "max": None, "avg": None}
    return {"min": round(min(vals), 2), "max": round(max(vals), 2), "avg": round(sum(vals) / len(vals), 2)}


switch_rows = results["switches"]
ftl = [r for r in switch_rows if r.get("direction") == "fast->long" and r.get("status") == "ok"]
ltf = [r for r in switch_rows if r.get("direction") == "long->fast" and r.get("status") == "ok"]

results["summary"] = {
    "cold_fast_ready_sec": results["cold_start"].get("fast", {}).get("ready_sec"),
    "cold_fast_first_response_sec": results["cold_start"].get("fast", {}).get("first_response_sec"),
    "cold_long_ready_sec": results["cold_start"].get("long", {}).get("ready_sec"),
    "cold_long_first_response_sec": results["cold_start"].get("long", {}).get("first_response_sec"),
    "fast_to_long_total_sec": stats([r.get("total_sec") for r in ftl]),
    "fast_to_long_ready_sec": stats([r.get("ready_sec") for r in ftl]),
    "long_to_fast_total_sec": stats([r.get("total_sec") for r in ltf]),
    "long_to_fast_ready_sec": stats([r.get("ready_sec") for r in ltf]),
    "oom_fast_to_long": sum(1 for r in ftl if r.get("oom")),
    "oom_long_to_fast": sum(1 for r in ltf if r.get("oom")),
    "recommendation": "",
}
s = results["summary"]
avg_total = max(
    s["fast_to_long_total_sec"]["avg"] or 0,
    s["long_to_fast_total_sec"]["avg"] or 0,
)
if avg_total <= 10:
    s["recommendation"] = "exclusive router 실용 가능 (평균 전환 <= 10초)"
elif avg_total <= 30:
    s["recommendation"] = "큰 요청에만 제한적 exclusive router 가능 (10~30초)"
else:
    s["recommendation"] = "수동 전환 또는 idle timeout 기반만 권장 (30초+)"
save()

print("\n=== MARKDOWN TABLE ===")
print("| Direction | Run | Ready sec | First response sec | GPU0 VRAM | GPU1 VRAM | RAM used | Note |")
print("|---|---:|---:|---:|---:|---:|---:|---|")
for profile, row in results.get("cold_start", {}).items():
    if row.get("status") == "ok":
        print(
            f"| cold-{profile} | 1 | {row.get('ready_sec', '-')} | {row.get('first_response_sec', '-')} | "
            f"{row.get('gpu0_vram_mib', '-')} | {row.get('gpu1_vram_mib', '-')} | {row.get('ram_used_mib', '-')} | cold start |"
        )
    else:
        print(f"| cold-{profile} | 1 | - | - | - | - | - | {row.get('note', 'fail')[:40]} |")
for r in switch_rows:
    if r.get("status") == "ok":
        print(
            f"| {r['direction']} | {r['run']} | {r.get('ready_sec', '-')} | {r.get('first_response_sec', '-')} | "
            f"{r.get('gpu0_vram_mib', '-')} | {r.get('gpu1_vram_mib', '-')} | {r.get('ram_used_mib', '-')} | ok |"
        )
    else:
        note = "OOM" if r.get("oom") else r.get("note", "fail")[:40]
        print(f"| {r['direction']} | {r['run']} | - | - | - | - | - | {note} |")

print("\n=== VRAM RELEASE ===")
for v in results["vram_release"]:
    print(
        f"after {v['after_profile']}: gpu0 released in {v['released_gpu0_sec']}s, "
        f"gpu1 released in {v['released_gpu1_sec']}s, idle={v['idle_gpu0_mib']}/{v['idle_gpu1_mib']} MiB"
    )

print("\n=== SUMMARY ===")
print(json.dumps(results["summary"], indent=2, ensure_ascii=False))
print(f"\n[raw] {out_dir}/results.json")
PY
