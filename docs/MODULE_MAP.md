# Module Map — Code Tiers

> `router/` 모듈을 **Build / Buy / Legacy / Reference** 기준으로 분류한다.  
> Build vs Buy 상세 → [INTEGRATIONS.md](./INTEGRATIONS.md)

---

## Tier 0 — Build (Core IP · `runtime_core/` + orchestrators)

**직접 개발 · Context Runtime v1 SKU**

| Module | Path | IP | Responsibility |
|--------|------|:--:|----------------|
| ContextNeed | `context_need.py` | ★★★★★ | intent · coverage_targets |
| BudgetPlan | `context_budget.py` | ★★★★★ | allocate_dynamic |
| Coverage | `coverage_checker.py` | ★★★★★ | truncate audit |
| Recovery | `recovery_scheduler.py` | ★★★★★ | bump · re-retrieve |
| Scheduler contract | `runtime_core/scheduler_contract.py` | ★★★★★ | Inputs/Outputs |
| Orchestrator | `dynamic_context_scheduler.py` | ★★★★★ | turn pipeline |
| Prompt pack policy | `prompt_builder.py` | ★★★★☆ | budget-aware pack |
| Message index | `message_index.py` | ★★★☆☆ | stable key diff |
| Failed action | `failed_action.py` | ★★★☆☆ | cold summary policy |
| Turn log | `runtime_turn_log.py` | ★★☆☆☆ | metrics schema |
| Inspector | `runtime_inspector.py` | ★★☆☆☆ | dev UX (schema Build) |
| Package helpers | `runtime_core/indexing_helpers.py` | ★★★☆☆ | section classify |
| Prompt enforcer | `runtime_core/prompt_enforcer.py` | ★★★☆☆ | last-resort guard |

Import (목표):

```python
from runtime_core.scheduler_contract import SchedulerInputs, SchedulerOutputs
from dynamic_context_scheduler import build_context_for_turn
```

---

## Tier 1 — Adapters (Buy glue · `adapters/`)

| Adapter | Buy | Legacy fallback | Env |
|---------|-----|-----------------|-----|
| `adapters/memory.py` | LangGraph checkpoint | `legacy/memory_store.py` | `LANGGRAPH_ENABLED` |
| `adapters/retrieval.py` | LlamaIndex | `legacy/retriever.py` | `VECTOR_RETRIEVAL` |
| `adapters/gateway.py` | LiteLLM | httpx direct | `LITELLM_ENABLED` |
| `adapters/trace.py` | OpenTelemetry | JSON backup | `OTEL_*` |
| `adapters/observe.py` | Langfuse | `legacy/agent_runs.py` | `LANGFUSE_*` |
| `adapters/langgraph.py` | LangGraph store | file store | `LANGGRAPH_ENABLED` |
| `adapters/mcp.py` | MCP (v2) | — | `MCP_ENABLED` |

Import (목표):

```python
from adapters.memory import SessionState, load_state
from adapters.retrieval import retrieve_for_need
from adapters.trace import begin_flow, record_proxy
```

---

## Tier 2 — Legacy (POC isolation · `legacy/`)

**재발명 지옥 방지 — adapter 뒤로만 접근**

| Module | Was | Migrate to |
|--------|-----|------------|
| `legacy/memory_store.py` | file session DB | LangGraph via `adapters/memory` |
| `legacy/retriever.py` | BM25 + vector merge | LlamaIndex via `adapters/retrieval` |
| `legacy/agent_runs.py` | SSE events | Langfuse via `adapters/observe` |
| `legacy/context_optimizer.py` | static ratios | dynamic scheduler |
| `legacy/runtime_optimizer.py` | blind truncate | coverage + recovery |

Top-level shims **삭제됨**. Application code → ``adapters/`` only. Legacy → ``legacy/`` (adapter backend).

---

## Tier 3 — Reference Integration (Agent Runtime v2)

**위치**: `router/reference/`

| Module | Responsibility |
|--------|----------------|
| `reference/planner.py` | AgentPlan |
| `reference/plan_state.py` | phase gate |
| `reference/agent_exec.py` | tool validation |
| `reference/evidence_*.py` | evidence layer |
| `reference/loop_guard.py` | final gate |

Agent graph → LangGraph (Buy). Reference = Cursor POC until v2 SKU.

---

## Tier 4 — Infrastructure (Ingress)

| Module | Responsibility | Note |
|--------|----------------|------|
| `main.py` | HTTP ingress | → `adapters/gateway` |
| `intent_router.py` | two-pass orchestration | uses adapters |
| `qwen_request.py` | engine kwargs | Buy engine |
| `context_cache.py` | ContextIndex | ingress cache |

---

## Tier 5 — Integrations (low-level stubs · `integrations/`)

| Module | Target |
|--------|--------|
| `integrations/llamaindex.py` | vector engine |
| `integrations/otel.py` | span export |
| `integrations/langfuse.py` | observe events |
| `integrations/flow_tracing.py` | OTel + JSON |

Used by `adapters/` — do not import from app code directly.

---

## Dockerfile COPY 우선순위

1. Tier 0 (Build) + Tier 1 (adapters)
2. Tier 3 (reference)
3. Tier 4 (ingress)
4. Tier 2 (legacy) + shims

```dockerfile
COPY runtime_core reference integrations legacy adapters /app/
```

---

## Tests by Tier

| Script | Tier |
|--------|------|
| `benchmark-dynamic-budget-matrix.py` | 0 |
| `benchmark-recovery-e2e.py` | 0 |
| `run-vector-e2e.sh` | 1 (adapter) |
| `test-evidence-judge.py` | 3 |
| `test-flow-tracing.py` | 1 |

*Last updated: 2026-06-18*
