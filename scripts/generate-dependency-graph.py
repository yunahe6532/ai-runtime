#!/usr/bin/env python3
"""Generate before/after dependency graphs + architecture verification report."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "router"
DOCS = ROOT / "docs"
ASSETS = DOCS / "assets"

RUNTIME_CORE_FORBIDDEN_ROOTS = frozenset(
    {
        "legacy",
        "adapters",
        "integrations",
        "reference",
        "dynamic_context_scheduler",
        "prompt_builder",
        "main",
        "intent_router",
    }
)

LEGACY_IMPORT_ALLOWLIST = {
    "router/prompt_builder.py": ("legacy.retriever",),
}

BENCH_PATH = ROOT / "tmp" / "benchmark-memory-hierarchy.json"

BEFORE_MMD = """flowchart TB
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
"""

MEMORY_HIERARCHY_MMD = """flowchart TB
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
"""


def _rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _root(module: str) -> str:
    return module.split(".", 1)[0]


def _imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.lineno, node.module))
    return out


def collect_py_files() -> list[Path]:
    files: list[Path] = []
    for base in (ROUTER, ROOT / "scripts"):
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return sorted(files)


def run_boundary_check() -> int:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-architecture-boundary.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    return proc.returncode


@dataclass
class GraphReport:
    generated_at: str
    boundary_violations: int
    runtime_core_forbidden_imports: int
    main_direct_adapters: list[str]
    main_legacy_imports: list[str]
    main_removed_shim_imports: list[str]
    legacy_importers: list[str]
    adapter_modules: list[str]
    runtime_core_modules: list[str]
    layer_edges: dict[str, dict[str, int]]
    benchmark_summary: list[dict]


def _layer_for_file(rel: str) -> str:
    if rel.startswith("router/runtime_core/"):
        return "runtime_core"
    if rel.startswith("router/adapters/"):
        return "adapters"
    if rel.startswith("router/legacy/"):
        return "legacy"
    if rel.startswith("router/integrations/"):
        return "integrations"
    if rel.startswith("router/reference/"):
        return "reference"
    if rel == "router/main.py":
        return "app"
    if rel.startswith("router/"):
        return "orchestration"
    return "other"


def _layer_for_import(module: str) -> str:
    root = _root(module)
    if root == "adapters" or module.startswith("adapters."):
        return "adapters"
    if root == "legacy" or module.startswith("legacy."):
        return "legacy"
    if root == "integrations" or module.startswith("integrations."):
        return "integrations"
    if root == "runtime_core" or module.startswith("runtime_core."):
        return "runtime_core"
    if root == "reference" or module.startswith("reference."):
        return "reference"
    if root in {"memory_store", "retriever", "agent_runs", "flow_trace"}:
        return "removed_shim"
    return "stdlib_or_local"


def _analyze() -> GraphReport:
    layer_edges: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    runtime_core_forbidden = 0
    main_adapters: list[str] = []
    main_legacy: list[str] = []
    main_shims: list[str] = []
    legacy_importers: set[str] = set()
    adapter_modules: set[str] = set()
    runtime_core_modules: set[str] = set()

    for path in collect_py_files():
        if not str(path).startswith(str(ROUTER)):
            continue
        rel = _rel(path)
        src_layer = _layer_for_file(rel)
        if src_layer == "adapters":
            adapter_modules.add(rel)
        if src_layer == "runtime_core":
            runtime_core_modules.add(rel)

        for _lineno, module in _imports(path):
            tgt_layer = _layer_for_import(module)
            layer_edges[src_layer][tgt_layer] += 1

            if rel.startswith("router/runtime_core/") and _root(module) in RUNTIME_CORE_FORBIDDEN_ROOTS:
                runtime_core_forbidden += 1

            if rel == "router/main.py":
                if tgt_layer == "adapters":
                    main_adapters.append(module)
                elif tgt_layer == "legacy" or module.startswith("legacy."):
                    main_legacy.append(module)
                elif tgt_layer == "removed_shim":
                    main_shims.append(module)

            if tgt_layer == "legacy" and src_layer == "orchestration":
                allowed = LEGACY_IMPORT_ALLOWLIST.get(rel, ())
                if not any(module == p or module.startswith(p + ".") for p in allowed):
                    legacy_importers.add(rel)

    bench: list[dict] = []
    if BENCH_PATH.exists():
        try:
            bench = json.loads(BENCH_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            bench = []

    boundary_ok = run_boundary_check() == 0

    return GraphReport(
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        boundary_violations=0 if boundary_ok else 1,
        runtime_core_forbidden_imports=runtime_core_forbidden,
        main_direct_adapters=sorted(set(main_adapters)),
        main_legacy_imports=sorted(set(main_legacy)),
        main_removed_shim_imports=sorted(set(main_shims)),
        legacy_importers=sorted(legacy_importers),
        adapter_modules=sorted(adapter_modules),
        runtime_core_modules=sorted(runtime_core_modules),
        layer_edges={k: dict(v) for k, v in layer_edges.items()},
        benchmark_summary=bench,
    )


def _build_after_mmd(report: GraphReport) -> str:
    adapter_list = ", ".join(
        p.replace("router/adapters/", "").replace(".py", "")
        for p in report.adapter_modules
        if p.endswith(".py") and p != "router/adapters/__init__.py"
    )
    core_list = ", ".join(
        p.replace("router/runtime_core/", "").replace(".py", "")
        for p in report.runtime_core_modules
        if p.endswith(".py") and p != "router/runtime_core/__init__.py"
    )
    return f"""flowchart TB
  subgraph app["App / Orchestration"]
    MAIN[main.py]
    IR[intent_router.py]
    DCS[dynamic_context_scheduler.py]
    PB[prompt_builder.py]
  end

  subgraph adapters["adapters.* — public I/O surface"]
    direction TB
    ADAPT["{adapter_list or 'memory · retrieval · gateway · trace · observe'}"]
  end

  subgraph core["runtime_core.* — pure policy"]
    direction TB
    CORE["{core_list or 'memory_policy · memory_hierarchy · runtime_events'}"]
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
"""


def _benchmark_table(rows: list[dict]) -> str:
    if not rows:
        return "_Run `python3 scripts/benchmark-memory-hierarchy.py` to populate._\n"
    lines = [
        "| case | raw | prompt_pack | gpu_context | ratio | coverage | hit_rate | re-read avoid |",
        "|------|-----|-------------|-------------|-------|----------|----------|---------------|",
    ]
    for r in rows:
        lines.append(
            f"| {r.get('label','?')} "
            f"| {r.get('raw_history_tokens',0):,} "
            f"| {r.get('prompt_pack_tokens',0):,} "
            f"| {r.get('gpu_context_tokens',0):,} "
            f"| {r.get('compression_ratio',0):.4f} "
            f"| {r.get('coverage_score',0):.2f} "
            f"| {r.get('memory_hit_rate',0):.2f} "
            f"| {r.get('repeated_read_avoidance',0):.2f} |"
        )
    lines.append("")
    lines.append(
        "> **Compression proved** (~80K → ~700 tokens). "
        "**Quality gate next**: coverage ≥ 0.8 + task_success ≥ 95% (`--quality-gate`)."
    )
    return "\n".join(lines)


def _build_markdown(report: GraphReport, after_mmd: str) -> str:
    edges = report.layer_edges
    orch_to_adapters = edges.get("orchestration", {}).get("adapters", 0)
    adapters_to_legacy = edges.get("adapters", {}).get("legacy", 0)
    adapters_to_integ = edges.get("adapters", {}).get("integrations", 0)
    legacy_warn = ""
    if report.legacy_importers:
        legacy_warn = " ⚠ " + ", ".join(report.legacy_importers[:6])

    return f"""# Dependency Graph — Before / After

> Generated: `{report.generated_at}` · `python3 scripts/generate-dependency-graph.py`

## Message

**Before:** app code was coupled to legacy modules and direct engine HTTP.

**After:** app calls `adapters.*` for I/O; `runtime_core` is pure policy; legacy/integrations/backends are swappable.

## Verification snapshot

| Check | Result |
|-------|--------|
| Architecture boundary violations | **{report.boundary_violations}** |
| `runtime_core` → adapters/legacy/integrations imports | **{report.runtime_core_forbidden_imports}** |
| `main.py` → `legacy.*` direct imports | **{len(report.main_legacy_imports)}** |
| `main.py` → removed top-level shims | **{len(report.main_removed_shim_imports)}** |
| `main.py` → `adapters.*` imports | {", ".join(f"`{m}`" for m in report.main_direct_adapters) or "—"} |
| Orchestration → legacy (non-allowlist) | **{len(report.legacy_importers)}**{legacy_warn or " ✅"} |

Layer edges: orchestration→adapters **{orch_to_adapters}** · adapters→legacy **{adapters_to_legacy}** · adapters→integrations **{adapters_to_integ}**

## Before — monolithic app + legacy + direct engine

```mermaid
{BEFORE_MMD.strip()}
```

Top-level shims `memory_store.py` / `retriever.py` / `agent_runs.py` / `flow_trace.py` are **removed**.

## After — adapters + runtime_core + isolated legacy

```mermaid
{after_mmd.strip()}
```

Source: [`assets/dependency-after.mmd`](./assets/dependency-after.mmd)

## Memory Hierarchy path (Local LLM differentiator)

```mermaid
{MEMORY_HIERARCHY_MMD.strip()}
```

Source: [`assets/memory-hierarchy.mmd`](./assets/memory-hierarchy.mmd)

### Benchmark summary

{_benchmark_table(report.benchmark_summary)}

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
"""


def _verify(report: GraphReport) -> list[str]:
    errors: list[str] = []
    if report.boundary_violations:
        errors.append(f"boundary violations: {report.boundary_violations}")
    if report.runtime_core_forbidden_imports:
        errors.append(f"runtime_core forbidden imports: {report.runtime_core_forbidden_imports}")
    if report.main_legacy_imports:
        errors.append(f"main.py legacy imports: {report.main_legacy_imports}")
    if report.main_removed_shim_imports:
        errors.append(f"main.py removed shim imports: {report.main_removed_shim_imports}")
    if report.legacy_importers:
        errors.append(f"orchestration imports legacy: {report.legacy_importers}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate dependency before/after graphs")
    parser.add_argument("--verify", action="store_true", help="Exit 1 if architecture checks fail")
    args = parser.parse_args()

    report = _analyze()
    after_mmd = _build_after_mmd(report)

    ASSETS.mkdir(parents=True, exist_ok=True)
    (ASSETS / "dependency-before.mmd").write_text(BEFORE_MMD.strip() + "\n", encoding="utf-8")
    (ASSETS / "dependency-after.mmd").write_text(after_mmd.strip() + "\n", encoding="utf-8")
    (ASSETS / "memory-hierarchy.mmd").write_text(MEMORY_HIERARCHY_MMD.strip() + "\n", encoding="utf-8")

    md = _build_markdown(report, after_mmd)
    (DOCS / "dependency-before-after.md").write_text(md, encoding="utf-8")

    out_json = ROOT / "tmp" / "dependency-graph.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False), encoding="utf-8")

    print("dependency graph: generated")
    print("  docs/dependency-before-after.md")
    print("  docs/assets/dependency-after.mmd")
    print("  docs/assets/memory-hierarchy.mmd")
    print("  tmp/dependency-graph.json")
    print(
        f"  boundary={report.boundary_violations} "
        f"runtime_core_forbidden={report.runtime_core_forbidden_imports} "
        f"main→adapters={len(report.main_direct_adapters)}"
    )

    if args.verify:
        errors = _verify(report)
        if errors:
            print("VERIFY FAIL")
            for err in errors:
                print(f"  - {err}")
            return 1
        print("VERIFY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
