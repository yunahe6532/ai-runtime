#!/usr/bin/env python3
"""Legacy context_optimizer offline test — skipped unless LEGACY_OPTIMIZER=1."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

if os.getenv("LEGACY_OPTIMIZER", "0") != "1":
    print("SKIP: legacy context_optimizer archived. Set LEGACY_OPTIMIZER=1 to run against archive copy.")
    sys.exit(0)

ARCHIVE_COPY = (
    Path.home()
    / ".local/share/ai-runtime/archive/deprecated/20260622/legacy/context_optimizer.py"
)


def _load_archived():
    if not ARCHIVE_COPY.is_file():
        print(f"SKIP: archive copy missing: {ARCHIVE_COPY}")
        sys.exit(0)
    spec = importlib.util.spec_from_file_location("legacy_context_optimizer_archived", ARCHIVE_COPY)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {ARCHIVE_COPY}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    import json  # noqa: E402

    archived = _load_archived()
    ContextOptimizer = archived.ContextOptimizer
    _est_body_tokens = archived._est_body_tokens
    _est_message_tokens = archived._est_message_tokens

    capture_dir = Path(os.getenv("CAPTURE_HOST_DIR") or (Path.home() / ".local/share/ai-runtime/captures"))
    files = sorted(capture_dir.glob("*.request.json"))
    if not files:
        print(f"no captures in {capture_dir}")
        return 1

    opt = ContextOptimizer()
    print(f"testing {len(files)} captures from archive copy\n")
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
