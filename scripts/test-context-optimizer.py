#!/usr/bin/env python3
"""Legacy context_optimizer offline test — skipped unless LEGACY_OPTIMIZER=1."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "router"))

if os.getenv("LEGACY_OPTIMIZER", "0") != "1":
    print("SKIP: legacy context_optimizer removed from default path. Set LEGACY_OPTIMIZER=1 to run.")
    sys.exit(0)

import json  # noqa: E402

from legacy.context_optimizer import ContextOptimizer, _est_body_tokens, _est_message_tokens  # noqa: E402


def main() -> int:
    capture_dir = Path(__file__).resolve().parents[1] / "tmp" / "cursor-captures"
    files = sorted(capture_dir.glob("*.request.json"))
    if not files:
        print(f"no captures in {capture_dir}")
        return 1

    opt = ContextOptimizer()
    print(f"testing {len(files)} captures\n")
    for f in files:
        payload = json.loads(f.read_text(encoding="utf-8"))
        body = payload["body"]
        optimized, stats = opt.optimize(body)
        route = "fast" if _est_message_tokens(optimized) <= 20000 else "long"
        print(
            f"{f.stem}: mode={stats.mode} req={stats.request_num} "
            f"raw={stats.raw_tokens} opt={stats.optimized_tokens} saved={stats.saved_pct}% route={route}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
