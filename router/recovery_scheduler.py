"""Recovery scheduler — budget bump, re-retrieve, prompt rebuild on coverage fail."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from context_budget import BudgetPlan, RetrievalStats, allocate_dynamic
from coverage_checker import CoverageReport

LOG = logging.getLogger("router.recovery_scheduler")

MAX_RECOVERY_ROUNDS = int(__import__("os").getenv("MAX_RECOVERY_ROUNDS", "2"))
BUDGET_BUMP_RATIO = float(__import__("os").getenv("RECOVERY_BUDGET_BUMP", "1.25"))


@dataclass
class RecoveryResult:
    budget: BudgetPlan
    retrieval_pack: Any = None
    prompt_pack: Any = None
    coverage: CoverageReport | None = None
    rounds: int = 0
    recovered: bool = False
    actions: list[str] = field(default_factory=list)


class RecoveryScheduler:
    def increase_budget(self, budget: BudgetPlan, *, ratio: float = BUDGET_BUMP_RATIO) -> BudgetPlan:
        bumped = BudgetPlan(
            total=int(budget.total * ratio),
            system=int(budget.system * ratio),
            plan=int(budget.plan * ratio),
            state=int(budget.state * ratio),
            delta=int(budget.delta * ratio),
            session_tail=int(budget.session_tail * ratio),
            retrieved=int(budget.retrieved * ratio),
            artifact=int(budget.artifact * ratio),
            current_task=int(budget.current_task * ratio),
            output_reserved=budget.output_reserved,
            backend=budget.backend,
            phase=budget.phase,
            mode=budget.mode + "+recovery",
        )
        return bumped

    def recover(
        self,
        *,
        context_need: Any,
        budget: BudgetPlan,
        retrieval_pack: Any,
        coverage: CoverageReport,
        retrieve_fn: Any,
        build_fn: Any,
        retrieve_kwargs: dict[str, Any],
        build_kwargs: dict[str, Any],
    ) -> RecoveryResult:
        """Run recovery loop: bump budget → re-retrieve → rebuild → re-check."""
        from coverage_checker import check_coverage

        result = RecoveryResult(budget=budget, retrieval_pack=retrieval_pack, coverage=coverage)
        current_budget = budget
        current_pack = retrieval_pack
        current_prompt = None
        current_coverage = coverage

        for rnd in range(MAX_RECOVERY_ROUNDS):
            if current_coverage.complete:
                result.recovered = True
                break

            action = current_coverage.action
            result.actions.append(f"round_{rnd + 1}:{action}")
            LOG.info(
                "recovery round=%d action=%s score=%.2f missing=%s",
                rnd + 1,
                action,
                current_coverage.coverage_score,
                current_coverage.missing[:3],
            )

            if action in ("increase_budget", "re_retrieve"):
                current_budget = self.increase_budget(current_budget)
                stats = RetrievalStats.from_pack(current_pack)
                current_budget = allocate_dynamic(
                    current_budget.backend,
                    current_budget.phase,
                    current_budget.output_reserved,
                    context_need,
                    stats,
                )
                hints: dict[str, Any] = {}
                st = retrieve_kwargs.get("state")
                if st is not None:
                    try:
                        from runtime_core.evidence_cluster import recovery_retrieval_hints

                        ws = getattr(st, "workspace_path", "") or ""
                        hints = recovery_retrieval_hints(
                            st, context_need, current_coverage, workspace=ws,
                        )
                    except ImportError:
                        hints = {}
                retrieve_kwargs = {
                    **retrieve_kwargs,
                    "budget_tokens": current_budget.retrieved,
                    "force_refresh": True,
                    **hints,
                }
                current_pack = retrieve_fn(**retrieve_kwargs)

            build_kwargs = {
                **build_kwargs,
                "budget_plan": current_budget,
                "retrieval_pack": current_pack,
            }
            current_prompt = build_fn(**build_kwargs)
            ap = {}
            st = build_kwargs.get("state")
            if st is not None:
                ap = getattr(st, "agent_plan", None) or {}
            current_coverage = check_coverage(
                context_need,
                current_pack,
                current_prompt,
                truncation_markers=getattr(current_prompt, "truncation_markers", None),
                evidence_needed=list(ap.get("evidence_needed") or []),
                evidence_collected=list(ap.get("evidence_collected") or []),
                source_hits=list(ap.get("source_hits") or []),
                coverage_hits=list(ap.get("coverage_hits") or []),
            )
            result.rounds = rnd + 1

            if current_coverage.complete:
                result.recovered = True
                break
            if action == "ask_tool":
                break

        result.budget = current_budget
        result.retrieval_pack = current_pack
        result.prompt_pack = current_prompt
        result.coverage = current_coverage
        return result
