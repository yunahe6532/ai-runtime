#!/usr/bin/env python3
"""Regression: compressed pack routes to fast; long only above TOKEN_THRESHOLD."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from intent_router import IntentResult, route_backend  # noqa: E402


def _intent(**overrides) -> IntentResult:
    base = dict(
        intent="read_only_analysis",
        route="main",
        needs_tools=True,
        needs_files=False,
        needs_shell=False,
        needs_prior_summary=False,
        needs_raw_tool_results=False,
        needs_full_raw_context=False,
        context_budget_tokens=8000,
        context_pack=["current_query"],
        reason="test",
    )
    base.update(overrides)
    return IntentResult(**base)


def test_tool_planning_read_only_uses_fast() -> None:
    intent = _intent()
    backend, reason = route_backend(
        intent, pack_tokens=1510, sticky_long=True, agent_phase="tool_planning", active_backend=""
    )
    assert backend == "fast", (backend, reason)
    assert "fast" in reason
    print("test_tool_planning_read_only_uses_fast: OK")


def test_tool_planning_read_only_overrides_warm_long() -> None:
    intent = _intent()
    backend, reason = route_backend(
        intent, pack_tokens=1510, sticky_long=True, agent_phase="tool_planning", active_backend="long"
    )
    assert backend == "fast", (backend, reason)
    assert reason == "tool_planning_read_only_fast"
    print("test_tool_planning_read_only_overrides_warm_long: OK")


def test_compressed_final_answer_uses_fast() -> None:
    intent = _intent()
    backend, reason = route_backend(intent, pack_tokens=1510, sticky_long=True, agent_phase="final_answer")
    assert backend == "fast", (backend, reason)
    assert reason == "compressed_pack_fast"
    print("test_compressed_final_answer_uses_fast: OK")


def test_large_pack_uses_long() -> None:
    intent = _intent(route="main")
    backend, reason = route_backend(intent, pack_tokens=25000, sticky_long=False, agent_phase="final_answer")
    assert backend == "long", (backend, reason)
    assert reason == "pack_exceeds_threshold"
    print("test_large_pack_uses_long: OK")


def main() -> int:
    failed = 0
    for fn in (
        test_tool_planning_read_only_uses_fast,
        test_tool_planning_read_only_overrides_warm_long,
        test_compressed_final_answer_uses_fast,
        test_large_pack_uses_long,
    ):
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}", file=sys.stderr)
    print(f'{{"passed": {4 - failed}, "failed": {failed}, "total": 4}}')
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
