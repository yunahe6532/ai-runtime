# Integrations — Build vs Buy

> **원칙**: 프레임워크를 새로 만드는 게 아니라, **기존 프레임워크 위에서 Context Scheduling Policy를 파는 것.**  
> **Build** = `runtime_core/` · **Buy glue** = `adapters/` · **POC legacy** = `legacy/` (격리 · 점진 이전)

---

## Layer별 Build vs Buy

| Layer | Build? | Buy (Adapter) | Reason |
|-------|:------:|---------------|--------|
| OpenAI Gateway | ❌ | LiteLLM | provider routing · spend tracking 존재 |
| Provider Routing | ❌ | LiteLLM / OpenRouter | 재발명 금지 |
| Agent State Graph | ❌ | LangGraph | v2 reference only |
| Session Checkpoint | ❌ | LangGraph checkpointer | persistence Buy · delta policy Build |
| Vector Retrieval Engine | ❌ | LlamaIndex / Haystack | ingestion · hybrid search Buy |
| Observability Export | ❌ | OpenTelemetry | trace 표준 |
| Dashboard UI | ❌ | Langfuse / Phoenix | UI Buy · metric schema Build |
| Prompt/Provider Cache | ❌ | Provider / vLLM prefix cache | v3 scope |
| Inference Runtime | ❌ | llama.cpp · vLLM · API | tensor 연산 Buy |
| **Context Scheduler** | ✅ | — | 핵심 차별 · 직접 경쟁 없음 |
| **Dynamic Budget** | ✅ | — | Need→Measure→Allocate |
| **Coverage Engine** | ✅ | — | truncate audit · symbol |
| **Recovery Scheduler** | ✅ | — | fail→bump→re-retrieve |
| **Budget-aware Prompt Pack** | ✅ | — | template 아님 · slot pack |
| **Delta Context Policy** | ✅ | — | Cursor/local 특화 scheduling |
| **Retrieval Budget Policy** | ✅ | — | rank/ceiling · engine은 Buy |

---

## 영역 × Build/Buy (투자자용)

| 영역 | Build | Buy |
|------|:-----:|:---:|
| Vector engine | ❌ | LlamaIndex |
| Tracing | ❌ | OTel |
| Prompt cache | ❌ | Provider |
| Memory graph / checkpoint | ❌ | LangGraph |
| OpenAI gateway | ❌ | LiteLLM |
| Agent workflow | ❌ | LangGraph (v2) |
| Dashboard | ❌ | Langfuse |
| **Runtime Scheduler** | ✅ | — |
| **Coverage Engine** | ✅ | — |
| **Recovery Loop** | ✅ | — |
| **Dynamic Budget** | ✅ | — |

---

## 같은 계층끼리 비교 (Why not X?)

| 계층 | They do | We do (Policy) |
|------|---------|----------------|
| **Memory Runtime** ↔ LangGraph / Letta | State graph · checkpoint · persistence | **Context-aware Memory Scheduling** on top |
| **OpenAI Proxy** ↔ LiteLLM / OpenRouter | OpenAI-compatible gateway · routing | **Runtime Policy Layer** above gateway |
| **Vector Retrieval** ↔ LlamaIndex / Haystack | Index · embed · retrieve | **Retrieval Budget Policy** |
| **Inference** ↔ vLLM / llama.cpp | Forward · sampling | Adapter only |
| **Scheduler** ↔ *(none)* | — | **Context Scheduler** (IP) |

> Coverage/Recovery는 Proxy 기능이 **아님**. LiteLLM과 같은 축으로 비교하면 안 됨.

---

## 코드 레이아웃

```text
runtime_core/              ← Build (Core IP)
  context_need.py
  context_budget.py
  coverage_checker.py
  recovery_scheduler.py
  dynamic_context_scheduler.py
  scheduler_contract.py      ← Scheduler Inputs/Outputs
  prompt_builder.py          ← budget-aware pack policy

adapters/                  ← Buy glue
  memory.py                  LangGraph checkpoint optional
  retrieval.py               LlamaIndex + legacy BM25
  gateway.py                 LiteLLM optional
  trace.py                   OTel
  observe.py                 Langfuse + legacy SSE
  langgraph.py               checkpointer stub → wire
  mcp.py                     v2 stub

legacy/                    ← POC self-impl (격리 · migrate out)
  memory_store.py
  retriever.py
  agent_runs.py
  context_optimizer.py
  runtime_optimizer.py

reference/                 ← Cursor Agent POC (v2 SKU)
integrations/              ← llamaindex · otel · langfuse
```

Top-level shims **삭제됨**. Application → `adapters/` · backend → `legacy/`.

---

## Core IP (직접 유지 · `runtime_core/` + flat orchestrators)

| 모듈 | 파일 | IP |
|------|------|-----|
| ContextNeed | `context_need.py` | ★★★★★ |
| Dynamic Budget | `context_budget.py` | ★★★★★ |
| Coverage | `coverage_checker.py` | ★★★★★ |
| Recovery | `recovery_scheduler.py` | ★★★★★ |
| Scheduler contract | `runtime_core/scheduler_contract.py` | ★★★★★ |
| Orchestrator | `dynamic_context_scheduler.py` | ★★★★★ |
| Prompt Pack Policy | `prompt_builder.py` | ★★★★☆ |
| Delta policy | `message_index.py` + adapter | ★★★★☆ |
| Turn Log / Inspector schema | `runtime_turn_log.py` | ★★☆☆☆ |

---

## Adapters (Buy · `adapters/`)

| Adapter | Buy | Env | Status |
|---------|-----|-----|:------:|
| `adapters/retrieval.py` | LlamaIndex (+ legacy BM25) | `VECTOR_RETRIEVAL=1` `LLAMAINDEX_ENABLED=1` | ✅ |
| `adapters/trace.py` | OpenTelemetry | `OTEL_FLOW_TRACE=1` `OTEL_EXPORTER_OTLP_ENDPOINT` | ✅ |
| `adapters/observe.py` | Langfuse + legacy SSE | `LANGFUSE_*` | ✅ |
| `adapters/gateway.py` | LiteLLM / llama.cpp | `GATEWAY_BACKEND` / `BACKEND` | ✅ wired in main |
| `adapters/memory.py` | LangGraph checkpointer | `MEMORY_BACKEND=langgraph` | ✅ wired |
| `integrations/langgraph_memory.py` | LangGraph store + checkpoint | `pip install langgraph` | ✅ wired |
| `adapters/mcp.py` | MCP tools | `MCP_ENABLED=1` | v2 |

---

## Legacy (격리 · `legacy/`)

| Module | Was | Now | Migrate to |
|--------|-----|-----|------------|
| `legacy/memory_store.py` | self-impl DB | adapter backend | LangGraph store |
| `legacy/retriever.py` | self-impl search | engine wrapper | LlamaIndex only |
| `legacy/agent_runs.py` | self-impl SSE | observe shim | Langfuse primary |
| `legacy/context_optimizer.py` | static ratios | deprecated | dynamic scheduler |
| `legacy/runtime_optimizer.py` | truncate only | deprecated | coverage + recovery |

```bash
# Adapters
VECTOR_RETRIEVAL=1
LLAMAINDEX_ENABLED=0|1
OTEL_FLOW_TRACE=1
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces
LITELLM_ENABLED=0|1
LITELLM_URL=http://127.0.0.1:4000
LANGGRAPH_ENABLED=0|1
FLOW_TRACE=1                # optional .flow.json backup (legacy)
```

---

## Reference Integration (v2 Agent Runtime)

Cursor 검증용 — **v1 Context Runtime SKU 필수 아님.**

| Module | Role |
|--------|------|
| `reference/planner.py` | AgentPlan |
| `reference/evidence_judge.py` | sufficiency |
| `reference/loop_guard.py` | final gate |
| `reference/agent_exec.py` | tool validation |

Agent graph → LangGraph (Buy). Judge/guard policy → reference until v2 SKU.

---

## 현실적 스택 (Target)

```text
IDE / Agent
   ↓
LiteLLM-compatible Gateway          (adapters/gateway — Buy)
   ↓
AI Runtime Policy Layer             (runtime_core — Build)
   ├─ ContextNeed
   ├─ DynamicBudget / SchedulerOutputs
   ├─ CoverageChecker
   ├─ RecoveryScheduler
   └─ PromptPackPolicy
   ↓
Adapters                            (adapters/ — Buy glue)
   ├─ LangGraph memory/checkpoint
   ├─ LlamaIndex retrieval
   ├─ OpenTelemetry trace
   ├─ Langfuse observe
   └─ MCP tools (v2)
   ↓
LLM Engine / API                    (Buy)
```

---

## Adapter 구현 순서

1. ✅ OpenTelemetry — `integrations/otel.py` → `adapters/trace.py`
2. ✅ Langfuse — `integrations/langfuse.py` → `adapters/observe.py`
3. ✅ LlamaIndex — `integrations/llamaindex.py` → `adapters/retrieval.py`
4. ✅ Legacy isolation — `memory_store` · `retriever` · `agent_runs` → `legacy/`
5. ▶ LiteLLM gateway — `LITELLM_ENABLED=1` in production path
6. ▶ LangGraph checkpointer — replace file store backend
7. ▶ MCP — v2 Agent Runtime

E2E:

```bash
python3 scripts/check-architecture-boundary.py
python3 scripts/benchmark-gateway-swap.py           # BACKEND=mock (CI) or GATEWAY_LIVE=1
python3 scripts/benchmark-retriever-swap.py           # legacy BM25 vs LlamaIndex (same adapter API)
python3 scripts/test-observability-export.py        # OTel + turn log smoke
bash scripts/run-vector-e2e.sh
python3 scripts/benchmark-dynamic-budget-matrix.py
python3 scripts/benchmark-recovery-e2e.py
```

### Architecture boundary rules (enforced)

| Layer | Rule |
|-------|------|
| `runtime_core/` | `legacy` · `adapters` · `integrations` · `reference` import 금지 |
| orchestration | `legacy` import 금지 — **예외**: `prompt_builder` → `legacy.retriever` format only |
| `reference/` | `legacy` · `integrations` import 금지 → `adapters` 사용 |
| `legacy/` | `adapters/` · `integrations/` · legacy tests만 import 허용 |
| top-level shim | `memory_store.py` 등 존재 시 FAIL |

---

## Anti-patterns (삽질 방지)

| 하지 말 것 | 이유 | 대신 |
|-----------|------|------|
| memory DB 직접 확장 | LangGraph/Letta와 중복 | `adapters/memory.py` |
| vector engine 직접 | LlamaIndex 재발명 | `adapters/retrieval.py` |
| 자체 trace 포맷 | 대시보드 연동 막힘 | OTel span |
| OpenAI proxy 유지보수 | provider별 payload 지옥 | LiteLLM |
| ReAct loop 직접 | LangGraph 중복 | reference only |

*Last updated: 2026-06-18*
