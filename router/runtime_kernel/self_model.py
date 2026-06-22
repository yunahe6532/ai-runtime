"""Runtime Self Model — YAML → prompt block for AI Planner."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

LOG = logging.getLogger("runtime_kernel.self_model")

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "runtime_self_model.yaml"


@lru_cache(maxsize=1)
def load_self_model(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        LOG.warning("runtime_self_model missing path=%s", p)
        return {}
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except ImportError:
        LOG.warning("PyYAML not installed — self_model load skipped")
        return _fallback_self_model()
    except Exception as exc:
        LOG.warning("self_model load failed: %s", exc)
        return _fallback_self_model()


def _fallback_self_model() -> dict[str, Any]:
    return {
        "runtime_identity": {
            "role": "cursor-local-llm-runtime",
            "mode": "agent_proxy",
            "responsibilities": [
                "reduce Cursor context via delta/artifact memory",
                "preserve tool evidence",
                "decide retrieval/summary within budget",
                "block premature final answers",
            ],
        },
    }


def format_self_model_block(*, max_chars: int = 2000, path: str | Path | None = None) -> str:
    """Compact block for system prompt / RuntimeState."""
    model = load_self_model(path)
    if not model:
        return ""
    lines = ["[Runtime Self Model]"]
    ident = model.get("runtime_identity") or {}
    if ident:
        lines.append(f"role: {ident.get('role', '')}")
        lines.append(f"mode: {ident.get('mode', '')}")
        for r in ident.get("responsibilities") or []:
            lines.append(f"- {r}")
    guards = (model.get("hard_guards") or [])[:6]
    if guards:
        lines.append("hard_guards:")
        for g in guards:
            lines.append(f"- {g}")
    text = "\n".join(lines)
    return text[:max_chars] if len(text) > max_chars else text
