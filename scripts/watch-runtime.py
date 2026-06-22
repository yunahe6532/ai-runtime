#!/usr/bin/env python3
"""Live runtime viewer — Cursor-style thinking / tools / results.

실시간 감시 (권장):
  Terminal A:  python3 scripts/watch-runtime.py
  Terminal B:  Cursor에서 질문 / 또는 benchmark·e2e 테스트 실행

처음부터 + 실시간:
  python3 scripts/watch-runtime.py --from-start
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAIL = ROOT / "scripts" / "tail-explorer-flow.py"


def main() -> int:
    argv = [sys.executable, str(TAIL), "--live", "--follow", "--from-start", *sys.argv[1:]]
    return subprocess.call(argv, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
