"""Memory hierarchy metrics — funnel from raw history to GPU context."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class MemoryHierarchySnapshot:
    raw_history_tokens: int = 0
    stored_memory_items: int = 0
    stored_memory_tokens: int = 0
    retrieved_memory_tokens: int = 0
    prompt_pack_tokens: int = 0
    gpu_context_tokens: int = 0
    compression_ratio: float = 0.0
    memory_hit_rate: float = 0.0
    repeated_read_avoidance: float = 0.0
    coverage_score: float = 0.0
    tiers: dict[str, int] = field(default_factory=dict)
    funnel: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ratio(num: int, den: int) -> float:
    if den <= 0:
        return 0.0
    return round(num / den, 4)


def compute_memory_hierarchy(
    *,
    raw_history_tokens: int,
    stored_memory_items: int,
    stored_memory_tokens: int,
    retrieved_memory_tokens: int,
    prompt_pack_tokens: int,
    gpu_context_tokens: int,
    coverage_score: float = 0.0,
    memory_hit_rate: float = 0.0,
    repeated_read_avoidance: float = 0.0,
    tier_tokens: dict[str, int] | None = None,
) -> MemoryHierarchySnapshot:
    raw = max(0, int(raw_history_tokens))
    prompt = max(0, int(prompt_pack_tokens))
    gpu = max(0, int(gpu_context_tokens))
    snap = MemoryHierarchySnapshot(
        raw_history_tokens=raw,
        stored_memory_items=max(0, int(stored_memory_items)),
        stored_memory_tokens=max(0, int(stored_memory_tokens)),
        retrieved_memory_tokens=max(0, int(retrieved_memory_tokens)),
        prompt_pack_tokens=prompt,
        gpu_context_tokens=gpu,
        compression_ratio=_ratio(prompt, raw) if raw else 1.0,
        memory_hit_rate=round(float(memory_hit_rate), 4),
        repeated_read_avoidance=round(float(repeated_read_avoidance), 4),
        coverage_score=round(float(coverage_score), 4),
        tiers=dict(tier_tokens or {}),
    )
    snap.funnel = [
        {"stage": "raw_history", "tokens": snap.raw_history_tokens},
        {"stage": "stored_memory", "tokens": snap.stored_memory_tokens, "items": snap.stored_memory_items},
        {"stage": "retrieved_memory", "tokens": snap.retrieved_memory_tokens},
        {"stage": "prompt_pack", "tokens": snap.prompt_pack_tokens},
        {"stage": "gpu_context", "tokens": snap.gpu_context_tokens},
    ]
    return snap


def memory_hit_rate(*, targets: list[str], hits: list[str]) -> float:
    if not targets:
        return 1.0
    hit_set = {str(h).lower() for h in hits if h}
    found = sum(1 for t in targets if any(t.lower() in h or h in t.lower() for h in hit_set))
    return found / len(targets)


def repeated_read_avoidance(
    read_counts: dict[str, int],
    *,
    avoidance_stats: dict[str, int] | None = None,
) -> float:
    if avoidance_stats:
        attempts = int(avoidance_stats.get("attempts", 0) or 0)
        avoided = int(avoidance_stats.get("avoided", 0) or 0)
        if attempts > 0:
            return round(avoided / attempts, 4)
    if not read_counts:
        return 1.0
    total = sum(int(v or 0) for v in read_counts.values())
    repeats = sum(max(0, int(v or 0) - 1) for v in read_counts.values())
    if total <= 0:
        return 1.0
    return max(0.0, 1.0 - (repeats / total))
