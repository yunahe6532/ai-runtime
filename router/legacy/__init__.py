"""Legacy modules — deprecated, kept for archive and optional LEGACY_OPTIMIZER=1."""

from __future__ import annotations

import os

LEGACY_OPTIMIZER = os.getenv("LEGACY_OPTIMIZER", "0") == "1"

DEPRECATED_MODULES = (
    "memory_store",
    "retriever",
    "agent_runs",
)

ARCHIVED_MODULES = (
    "context_optimizer",
    "runtime_optimizer",
)


def legacy_optimizer_enabled() -> bool:
    return LEGACY_OPTIMIZER
