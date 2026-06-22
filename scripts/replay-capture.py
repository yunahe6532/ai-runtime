#!/usr/bin/env python3
"""
replay-capture.py — Cursor 캡처 요청을 로컬 router에 그대로 재전송하여 동작 확인.

사용법:
  python3 scripts/replay-capture.py [capture_file.request.json]
  python3 scripts/replay-capture.py  # 최신 캡처 자동 선택

옵션:
  --list        캡처 목록만 출력
  --turn N      대화 턴 번호 선택 (e.g. _0004)
  --router URL  라우터 주소 (기본: http://localhost:8080)
  --dry-run     요청 구조만 출력, 실제 전송 안 함
"""
import argparse
import glob
import json
import os
import sys
import time
import urllib.request
import urllib.error

CAPTURE_DIR = "/home/yunahe/ai-runtime/cursor-local-llm/tmp/cursor-captures"
DEFAULT_ROUTER = "http://localhost:8080"


def list_captures():
    files = sorted(glob.glob(f"{CAPTURE_DIR}/*.request.json"), key=os.path.getmtime, reverse=True)
    if not files:
        print("캡처 파일 없음. .env에서 CAPTURE_REQUESTS=1 설정 필요.")
        return []
    print(f"{'파일':<50} {'시간':>12} {'크기':>8}")
    print("-" * 75)
    for f in files[:20]:
        name = os.path.basename(f)
        mtime = time.strftime("%m-%d %H:%M:%S", time.localtime(os.path.getmtime(f)))
        size = os.path.getsize(f)
        print(f"{name:<50} {mtime:>12} {size:>7}B")
    return files


def load_capture(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # capture format: {"id": ..., "headers": {...}, "body": {...}}
    if "body" in data:
        return data["body"], data.get("headers", {})
    # fallback: treat entire file as body
    return data, {}


def summarize_body(body: dict):
    msgs = body.get("messages", [])
    tools = body.get("tools", [])
    print(f"\n{'='*60}")
    print(f"메시지 수: {len(msgs)}")
    print(f"도구 수: {len(tools)}")
    print(f"model: {body.get('model', '?')}")
    print(f"max_tokens: {body.get('max_tokens', '?')}")
    print(f"stream: {body.get('stream', '?')}")
    print()
    print("메시지 구조:")
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            continue
        role = m.get("role", "?")
        content = m.get("content", "")
        chars = len(str(content))
        tc = len(m.get("tool_calls") or [])
        tc_info = f" +{tc}tool_calls" if tc else ""
        last_marker = " ← LAST" if i == len(msgs) - 1 else ""
        print(f"  [{i:3d}] {role:<12} {chars:>6}chars{tc_info}{last_marker}")
    if tools:
        print(f"\n도구 목록: {[t.get('function',{}).get('name') for t in tools]}")
    print(f"{'='*60}\n")


def send_request(body: dict, headers: dict, router_url: str) -> dict:
    url = f"{router_url}/v1/chat/completions"
    # Force non-stream for easier output handling
    body = dict(body)
    body["stream"] = False
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req_headers = {
        "Content-Type": "application/json",
        "Authorization": headers.get("Authorization") or headers.get("authorization") or "Bearer sk-local",
    }
    req = urllib.request.Request(url, data=payload, headers=req_headers, method="POST")
    print(f"[→] POST {url}  ({len(payload)} bytes)")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            elapsed = time.time() - t0
            raw = resp.read()
            print(f"[←] HTTP {resp.status}  ({elapsed:.1f}s  {len(raw)} bytes)")
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        body_err = e.read().decode("utf-8", errors="replace")[:500]
        print(f"[!] HTTP {e.code}  ({elapsed:.1f}s): {body_err}")
        sys.exit(1)
    except Exception as e:
        print(f"[!] 요청 실패: {e}")
        sys.exit(1)


def print_response(resp: dict):
    print("\n[모델 응답]")
    try:
        msg = resp["choices"][0]["message"]
        role = msg.get("role", "?")
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []
        print(f"  role: {role}")
        if tool_calls:
            print(f"  tool_calls ({len(tool_calls)}):")
            for tc in tool_calls:
                fn = tc.get("function", {})
                args = str(fn.get("arguments", ""))[:200]
                print(f"    - {fn.get('name')}({args})")
        if content:
            print(f"  content ({len(content)} chars):")
            for line in content[:800].splitlines():
                print(f"    {line}")
            if len(content) > 800:
                print(f"    ... ({len(content) - 800} chars 생략)")
    except (KeyError, IndexError, TypeError) as e:
        print(f"  응답 파싱 오류: {e}")
        print(f"  raw: {str(resp)[:500]}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Cursor 캡처 요청 재전송")
    parser.add_argument("capture_file", nargs="?", help="캡처 파일 경로")
    parser.add_argument("--list", action="store_true", help="캡처 목록 출력")
    parser.add_argument("--turn", type=str, help="턴 번호 필터 (e.g. 0004)")
    parser.add_argument("--router", default=DEFAULT_ROUTER, help="라우터 URL")
    parser.add_argument("--dry-run", action="store_true", help="전송 없이 구조만 출력")
    args = parser.parse_args()

    if args.list:
        list_captures()
        return

    if args.capture_file:
        target = args.capture_file
    else:
        files = sorted(glob.glob(f"{CAPTURE_DIR}/*.request.json"), key=os.path.getmtime, reverse=True)
        if not files:
            print("[!] 캡처 파일 없음.")
            print("    .env에서 CAPTURE_REQUESTS=1 로 설정하거나")
            print("    도커 재시작 후 Cursor에서 요청 보내면 자동 캡처됩니다.")
            sys.exit(1)
        if args.turn:
            files = [f for f in files if f"_{args.turn.zfill(4)}" in f]
            if not files:
                print(f"[!] 턴 {args.turn}에 해당하는 캡처 없음")
                sys.exit(1)
        target = files[0]
        print(f"[i] 최신 캡처 선택: {os.path.basename(target)}")

    body, headers = load_capture(target)
    summarize_body(body)

    if args.dry_run:
        print("[dry-run] 전송 생략")
        return

    print(f"[i] 라우터: {args.router}")
    resp = send_request(body, headers, args.router)
    print_response(resp)


if __name__ == "__main__":
    main()
