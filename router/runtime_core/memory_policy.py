"""Memory hierarchy policy — pure rules for tiering and working-set selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MemoryTier(str, Enum):
    """LLM memory hierarchy tiers (cold → hot)."""

    SESSION = "session"  # recent dialogue / task state (RAM)
    ARTIFACT = "artifact"  # files, tool results (artifact store)
    VECTOR = "vector"  # long-term retrieval index
    POLICY = "policy"  # failed actions, preferences, bans
    GPU_HOT = "gpu_hot"  # final working set in GPU context


# Default inclusion priority for prompt pack (lower = evict first).
TIER_PROMPT_PRIORITY: dict[MemoryTier, int] = {
    MemoryTier.GPU_HOT: 0,
    MemoryTier.SESSION: 1,
    MemoryTier.ARTIFACT: 2,
    MemoryTier.VECTOR: 3,
    MemoryTier.POLICY: 4,
}

# Sources that must never go directly to GPU context as full blobs.
GPU_EXCLUDED_SOURCES = frozenset(
    {
        "full_history",
        "raw_tool_blob",
        "unindexed_file",
    }
)


@dataclass
class WorkingSetItem:
    tier: MemoryTier
    source: str
    tokens: int = 0
    priority: int = 0
    include_in_prompt: bool = True
    include_in_gpu: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkingSetPlan:
    items: list[WorkingSetItem] = field(default_factory=list)
    prompt_tokens: int = 0
    gpu_context_tokens: int = 0
    excluded_from_gpu: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "gpu_context_tokens": self.gpu_context_tokens,
            "excluded_from_gpu": list(self.excluded_from_gpu),
            "items": [
                {
                    "tier": i.tier.value,
                    "source": i.source,
                    "tokens": i.tokens,
                    "priority": i.priority,
                    "include_in_prompt": i.include_in_prompt,
                    "include_in_gpu": i.include_in_gpu,
                }
                for i in self.items
            ],
        }


@dataclass
class MemoryPolicy:
    """Decide what stays cold vs enters the GPU working set."""

    hot_session_tail_tokens: int = 4096
    max_artifact_tokens: int = 8192
    max_vector_tokens: int = 12000
    max_policy_tokens: int = 1024
    gpu_context_cap: int = 32768

    def classify_source(self, source: str) -> MemoryTier:
        src = (source or "").lower()
        if src in ("session", "session_tail", "delta", "current_task", "state"):
            return MemoryTier.SESSION
        if src in ("artifact", "tool_result", "file_read", "retrieved_code"):
            return MemoryTier.ARTIFACT
        if src in ("vector", "retrieved", "retrieval", "long_memory"):
            return MemoryTier.VECTOR
        if src in ("policy", "failed_action", "failed_tool"):
            return MemoryTier.POLICY
        return MemoryTier.ARTIFACT

    def prompt_inclusion_priority(self, tier: MemoryTier) -> int:
        return TIER_PROMPT_PRIORITY.get(tier, 99)

    def should_evict_to_cold(self, *, tier: MemoryTier, tokens: int, hot: bool = False) -> bool:
        if hot:
            return False
        caps = {
            MemoryTier.SESSION: self.hot_session_tail_tokens * 4,
            MemoryTier.ARTIFACT: self.max_artifact_tokens * 2,
            MemoryTier.VECTOR: self.max_vector_tokens * 2,
            MemoryTier.POLICY: self.max_policy_tokens * 2,
        }
        cap = caps.get(tier, 999999)
        return tokens > cap

    def tier_token_cap(self, tier: MemoryTier) -> int:
        return {
            MemoryTier.SESSION: self.hot_session_tail_tokens,
            MemoryTier.ARTIFACT: self.max_artifact_tokens,
            MemoryTier.VECTOR: self.max_vector_tokens,
            MemoryTier.POLICY: self.max_policy_tokens,
            MemoryTier.GPU_HOT: self.gpu_context_cap,
        }.get(tier, 0)

    def allow_in_gpu_context(self, *, source: str, tokens: int) -> bool:
        if source in GPU_EXCLUDED_SOURCES:
            return False
        return tokens <= self.gpu_context_cap

    def build_working_set(
        self,
        *,
        prompt_sources: dict[str, int],
        raw_history_tokens: int = 0,
    ) -> WorkingSetPlan:
        """Select working set from measured prompt source token map."""
        plan = WorkingSetPlan()
        ranked: list[tuple[int, str, MemoryTier, int]] = []
        for source, tokens in sorted((prompt_sources or {}).items(), key=lambda kv: -kv[1]):
            tok = int(tokens or 0)
            if tok <= 0:
                continue
            tier = self.classify_source(source)
            ranked.append((self.prompt_inclusion_priority(tier), source, tier, tok))

        ranked.sort(key=lambda row: (row[0], row[3]))
        gpu_used = 0
        prompt_used = 0

        for _prio, source, tier, tokens in ranked:
            cap = self.tier_token_cap(tier)
            alloc = min(tokens, cap) if cap else tokens
            in_gpu = self.allow_in_gpu_context(source=source, tokens=alloc)
            if in_gpu and gpu_used + alloc <= self.gpu_context_cap:
                gpu_used += alloc
            elif source in GPU_EXCLUDED_SOURCES or tokens > self.gpu_context_cap:
                plan.excluded_from_gpu.append(source)
            item = WorkingSetItem(
                tier=tier,
                source=source,
                tokens=alloc,
                priority=self.prompt_inclusion_priority(tier),
                include_in_prompt=True,
                include_in_gpu=in_gpu and alloc > 0,
            )
            plan.items.append(item)
            prompt_used += alloc

        if raw_history_tokens > prompt_used:
            plan.excluded_from_gpu.append("full_history")
        plan.prompt_tokens = prompt_used
        plan.gpu_context_tokens = gpu_used
        return plan
