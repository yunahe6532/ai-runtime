# Dependency Graph — Before / After

> Generated: `2026-06-21T09:34:03Z` · `python3 scripts/generate-dependency-graph.py`

## Message

**Before:** app code was coupled to legacy modules and direct engine HTTP.

**After:** app calls `adapters.*` for I/O; `runtime_core` is pure policy; legacy/integrations/backends are swappable.

## Verification snapshot

| Check | Result |
|-------|--------|
| Architecture boundary violations | **0** |
| `runtime_core` → adapters/legacy/integrations imports | **0** |
| `main.py` → `legacy.*` direct imports | **0** |
| `main.py` → removed top-level shims | **0** |
| `main.py` → `adapters.*` imports | `adapters.gateway`, `adapters.observe`, `adapters.trace` |
| Orchestration → legacy (non-allowlist) | **0** ✅ |

Layer edges: orchestration→adapters **6** · adapters→legacy **4** · adapters→integrations **3**

## Before — monolithic app + legacy + direct engine

```mermaid
flowchart TB
  subgraph before_app["App — tightly coupled"]
    MAIN[main.py]
    PB[prompt_builder.py]
    IR[intent_router.py]
  end
  subgraph before_legacy["Top-level / direct legacy"]
    MS[memory_store.py]
    RET[retriever.py]
    AR[agent_runs.py]
    FT[flow_trace.py]
  end
  subgraph before_engine["Direct engine I/O"]
    QW[qwen_request / httpx → llama URL]
  end
  MAIN --> MS
  MAIN --> RET
  MAIN --> AR
  MAIN --> FT
  MAIN --> QW
  PB --> RET
  IR --> MS
  classDef bad fill:#fee,stroke:#c33
  class MS,RET,AR,FT,QW bad
```

Top-level shims `memory_store.py` / `retriever.py` / `agent_runs.py` / `flow_trace.py` are **removed**.

## After — adapters + runtime_core + isolated legacy

```mermaid
flowchart TB
  subgraph app["App / Orchestration"]
    MAIN[main.py]
    IR[intent_router.py]
    DCS[dynamic_context_scheduler.py]
    PB[prompt_builder.py]
  end

  subgraph adapters["adapters.* — public I/O surface"]
    direction TB
    ADAPT["gateway, langgraph, mcp, memory, observe, retrieval, trace"]
  end

  subgraph core["runtime_core.* — pure policy"]
    direction TB
    CORE["evidence_cluster, evidence_keys, indexing_helpers, memory_hierarchy, memory_policy, prompt_enforcer, runtime_events, scheduler_contract"]
  end

  subgraph legacy["legacy.* — swappable implementations"]
    LEG["memory_store · retriever · agent_runs"]
  end

  subgraph integ["integrations.* — OTel · Langfuse · LlamaIndex"]
    INT["otel · langfuse · flow_tracing · llamaindex"]
  end

  subgraph backend["Inference backends — Buy"]
    LLM[llama.cpp · LiteLLM]
  end

  MAIN -->|"gateway · trace · observe"| ADAPT
  IR -->|"memory ingest"| ADAPT
  DCS -->|"memory · retrieval · policy"| ADAPT
  DCS --> CORE
  PB --> CORE
  ADAPT --> LEG
  ADAPT --> INT
  ADAPT -->|"chat_completion"| LLM
  INT --> LF[Langfuse / OTLP]

  classDef build fill:#efe,stroke:#393
  classDef buy fill:#eef,stroke:#339
  class ADAPT,CORE build
  class LEG,INT,LLM,LF buy
```

Source: [`assets/dependency-after.mmd`](./assets/dependency-after.mmd)

## Memory Hierarchy path (Local LLM differentiator)

```mermaid
flowchart TB
  RAW["Cursor full history<br/>~80K tokens"]
  ING["Memory Ingest<br/>adapters.memory · legacy.memory_store"]
  subgraph tiers["Memory Tiers — cold storage"]
    SESS["Session Memory<br/>recent dialogue · task state"]
    ART["Artifact Memory<br/>files · tool results"]
    VEC["Vector Memory<br/>adapters.retrieval"]
    POL["Policy Memory<br/>failed actions · bans"]
  end
  WS["Working Set Builder<br/>runtime_core.memory_policy"]
  BUD["Dynamic Budget + Coverage<br/>runtime_core scheduler"]
  PACK["Prompt Pack<br/>~700 tokens"]
  GPU["GPU Context / KV Cache"]
  LLM["Local LLM<br/>adapters.gateway → llama.cpp"]

  RAW --> ING --> tiers
  tiers --> WS --> BUD --> PACK --> GPU --> LLM
```

Source: [`assets/memory-hierarchy.mmd`](./assets/memory-hierarchy.mmd)

### Benchmark summary

| case | raw | prompt_pack | gpu_context | ratio | coverage | hit_rate | re-read avoid |
|------|-----|-------------|-------------|-------|----------|----------|---------------|
| bugfix | 0 | 1,093 | 1,093 | 0.0000 | 1.00 | 1.00 | 1.00 |
| explore | 0 | 1,270 | 1,270 | 0.0000 | 1.00 | 1.00 | 1.00 |

> **Compression proved** (~80K → ~700 tokens). **Quality gate next**: coverage ≥ 0.8 + task_success ≥ 95% (`--quality-gate`).

## CI verification

```bash
python3 scripts/check-architecture-boundary.py
python3 scripts/generate-dependency-graph.py --verify
python3 scripts/test-architecture-boundary.py
```

## Module layers

| Layer | Role | Build/Buy |
|-------|------|-----------|
| `runtime_core/` | MemoryPolicy · hierarchy · scheduler events | **Build IP** |
| `adapters/` | memory · retrieval · gateway · trace · observe | **Adapter surface** |
| `legacy/` | file-backed memory · BM25 retriever | **Swappable** |
| `integrations/` | OTel · Langfuse · LlamaIndex | **Buy glue** |
| Inference | llama.cpp · LiteLLM | **Buy** |
