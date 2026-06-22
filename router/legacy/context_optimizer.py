"""ARCHIVED 20260622 — see docs/reports/legacy-archive-applied.md.

Original: /home/yunahe/.local/share/ai-runtime/archive/deprecated/20260622/legacy/context_optimizer.py
Replacement: dynamic_context_scheduler + runtime_core.indexing_helpers
"""

_ARCHIVED = True
_ARCHIVE_PATH = "/home/yunahe/.local/share/ai-runtime/archive/deprecated/20260622/legacy/context_optimizer.py"


def __getattr__(name: str):
    raise ImportError(
        "legacy.context_optimizer archived; see docs/reports/legacy-archive-applied.md "
        f"({_ARCHIVE_PATH})"
    )
