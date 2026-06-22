"""Self-optimizing runtime: prompt budget enforcement + adaptive shrink on ctx failures.

.. deprecated::
    Legacy path (Tier 5). Prefer ``allocate_dynamic`` + ``coverage_checker`` +
    ``recovery_scheduler``. See ``docs/MODULE_MAP.md`` and ``router/legacy/``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from capture import _content_text
from context_budget import BACKEND_CTX_TOKENS, CTX_SAFETY_TOKENS, truncate_to_token_budget

LOG = logging.getLogger("router.runtime_optimizer")

_ENABLED = os.getenv("RUNTIME_OPTIMIZER", "1") == "1"
_STATE_FILE = Path(
    os.getenv(
        "RUNTIME_OPTIMIZER_STATE",
        str(Path(__file__).resolve().parents[1] / "tmp" / "context-cache" / "runtime_optimizer.json"),
    )
)
_SHRINK_STEP = float(os.getenv("RUNTIME_OPTIMIZER_SHRINK_STEP", "0.85"))
_MIN_TOOL_TAIL_CHARS = int(os.getenv("RUNTIME_OPTIMIZER_MIN_TOOL_CHARS", "400"))


@dataclass
class OptimizerState:
    ctx_overflow_count: int = 0
    ctx_success_count: int = 0
    tool_tail_char_factor: float = 1.0
    last_overflow_tokens: int = 0
    last_overflow_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def _est_body_tokens(body: dict[str, Any]) -> int:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return 0
    total = sum(_est_tokens(_content_text(m.get("content", ""))) for m in messages if isinstance(m, dict))
    tools = body.get("tools")
    if isinstance(tools, list):
        total += _est_tokens(json.dumps(tools, ensure_ascii=False))
    return total


class RuntimeOptimizer:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = self._load()

    def _load(self) -> OptimizerState:
        if not _STATE_FILE.exists():
            return OptimizerState()
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            return OptimizerState(**{k: data[k] for k in OptimizerState.__dataclass_fields__ if k in data})
        except (json.JSONDecodeError, OSError, TypeError):
            return OptimizerState()

    def _save(self) -> None:
        self._state.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(
            json.dumps(self._state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @property
    def state(self) -> OptimizerState:
        return self._state

    def record_ctx_overflow(self, prompt_tokens: int, n_ctx: int) -> None:
        if not _ENABLED:
            return
        with self._lock:
            self._state.ctx_overflow_count += 1
            self._state.last_overflow_tokens = prompt_tokens
            self._state.last_overflow_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._state.tool_tail_char_factor = max(
                _MIN_TOOL_TAIL_CHARS / max(1, int(os.getenv("TOOL_TAIL_MAX_CHARS", "1200"))),
                self._state.tool_tail_char_factor * _SHRINK_STEP,
            )
            self._save()
        LOG.warning(
            "runtime_optimizer ctx_overflow #%d prompt=%d n_ctx=%d tail_factor=%.2f",
            self._state.ctx_overflow_count,
            prompt_tokens,
            n_ctx,
            self._state.tool_tail_char_factor,
        )

    def record_ctx_success(self, prompt_tokens: int) -> None:
        if not _ENABLED:
            return
        with self._lock:
            self._state.ctx_success_count += 1
            if self._state.tool_tail_char_factor < 1.0 and prompt_tokens < BACKEND_CTX_TOKENS.get("long", 32768) * 0.5:
                self._state.tool_tail_char_factor = min(1.0, self._state.tool_tail_char_factor / _SHRINK_STEP)
            self._save()

    def _shrink_messages(self, messages: list[dict[str, Any]], budget_tokens: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        factor = self._state.tool_tail_char_factor
        base_tool_cap = int(int(os.getenv("TOOL_TAIL_MAX_CHARS", "1200")) * factor)
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            m = copy.deepcopy(msg)
            role = m.get("role")
            text = _content_text(m.get("content", ""))
            if role == "system":
                m["content"] = truncate_to_token_budget(text, max(512, budget_tokens // 3))
            elif role == "tool":
                cap = max(_MIN_TOOL_TAIL_CHARS, base_tool_cap)
                if len(text) > cap:
                    lines = text.splitlines()
                    m["content"] = (
                        f"[shrunk tool result lines={len(lines)} chars={len(text)}]\n"
                        + "\n".join(lines[:8])
                        + "\n...(truncated by runtime_optimizer)"
                    )
            elif role == "assistant" and m.get("tool_calls"):
                m["content"] = ""
            out.append(m)
        return out

    def enforce_prompt_budget(
        self,
        body: dict[str, Any],
        backend: str,
        phase: str = "tool_planning",
    ) -> tuple[dict[str, Any], int, bool]:
        """Return (body, est_tokens, was_shrunk)."""
        if not _ENABLED:
            return body, _est_body_tokens(body), False

        max_out = int(body.get("max_tokens") or 800)
        max_ctx = BACKEND_CTX_TOKENS.get(backend, BACKEND_CTX_TOKENS["long"])
        prompt_budget = max(4096, max_ctx - max_out - CTX_SAFETY_TOKENS)

        est = _est_body_tokens(body)
        if est <= prompt_budget:
            return body, est, False

        out = copy.deepcopy(body)
        messages = out.get("messages", [])
        if not isinstance(messages, list):
            return body, est, False

        shrunk = True
        # Pass 1: shrink tool/system content
        out["messages"] = self._shrink_messages(messages, prompt_budget)
        est = _est_body_tokens(out)

        # Pass 2: drop oldest non-user messages until within budget
        while est > prompt_budget and len(out["messages"]) > 2:
            msgs = out["messages"]
            drop_idx = next(
                (i for i, m in enumerate(msgs[:-1]) if isinstance(m, dict) and m.get("role") != "user"),
                0,
            )
            out["messages"] = msgs[:drop_idx] + msgs[drop_idx + 1 :]
            est = _est_body_tokens(out)

        # Pass 3: hard truncate system
        if est > prompt_budget:
            for m in out["messages"]:
                if isinstance(m, dict) and m.get("role") == "system":
                    m["content"] = truncate_to_token_budget(
                        _content_text(m.get("content", "")),
                        max(256, prompt_budget // 4),
                    )
            est = _est_body_tokens(out)

        LOG.info(
            "runtime_optimizer enforce backend=%s phase=%s est_before=%d est_after=%d budget=%d shrunk=%s",
            backend,
            phase,
            _est_body_tokens(body),
            est,
            prompt_budget,
            str(shrunk).lower(),
        )
        return out, est, shrunk

    def emergency_shrink(self, body: dict[str, Any], n_ctx: int) -> dict[str, Any]:
        """Aggressive shrink after llama exceed_context_size_error."""
        max_out = int(body.get("max_tokens") or 800)
        budget = max(2048, n_ctx - max_out - CTX_SAFETY_TOKENS)
        with self._lock:
            self._state.tool_tail_char_factor *= _SHRINK_STEP
            self._save()
        out, _, _ = self.enforce_prompt_budget(body, "long", "emergency")
        messages = out.get("messages", [])
        if isinstance(messages, list):
            out["messages"] = self._shrink_messages(messages, budget)
        return out


OPTIMIZER = RuntimeOptimizer()


def enforce_prompt_budget(
    body: dict[str, Any],
    backend: str,
    phase: str = "",
) -> tuple[dict[str, Any], bool]:
    optimized, _, shrunk = OPTIMIZER.enforce_prompt_budget(body, backend, phase)
    return optimized, shrunk


def record_ctx_overflow(prompt_tokens: int, n_ctx: int) -> None:
    OPTIMIZER.record_ctx_overflow(prompt_tokens, n_ctx)


def record_ctx_success(prompt_tokens: int) -> None:
    OPTIMIZER.record_ctx_success(prompt_tokens)


def emergency_shrink(body: dict[str, Any], n_ctx: int) -> dict[str, Any]:
    return OPTIMIZER.emergency_shrink(body, n_ctx)
