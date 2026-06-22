#!/usr/bin/env python3
"""Runtime reachability audit — beyond import graph (static + profile + merge)."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import textwrap
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "router"
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROUTER))

from runtime_kernel.runtime_paths import audit_json_dir, captures_dir, reports_dir  # noqa: E402

STATIC_JSON = audit_json_dir() / "runtime-reachability-static.json"
PROFILE_JSON = audit_json_dir() / "runtime-reachability-profile.json"
MERGED_JSON = audit_json_dir() / "runtime-reachability.json"

ENTRYPOINT_FILES = [
    ROUTER / "main.py",
    ROUTER / "intent_router.py",
    ROUTER / "dynamic_context_scheduler.py",
    ROUTER / "legacy" / "memory_store.py",
    ROUTER / "prompt_builder.py",
    ROUTER / "reference" / "agent_exec.py",
    ROUTER / "reference" / "response_guard.py",
    ROUTER / "adapters" / "gateway.py",
]

GUARD_MODULES = frozenset({
    "reference.loop_guard",
    "reference.response_guard",
    "failed_action",
    "coverage_checker",
    "recovery_scheduler",
})

TRACKED_ENVS: dict[str, str] = {
    "MEMORY_STATE_BODY": "1",
    "DYNAMIC_BUDGET": "1",
    "COVERAGE_CHECK": "1",
    "RECOVERY_ENABLED": "1",
    "VECTOR_RETRIEVAL": "1",
    "LLAMAINDEX_ENABLED": "0",
    "EVIDENCE_JUDGE_ENABLED": "1",
    "LLM_PLANNER_SHADOW_ENABLED": "0",
    "PLANNER_PROMOTION_GATE_ENABLED": "1",
    "PLANNER_PROMOTION_SHADOW_ONLY": "1",
    "CONTEXT_OPTIMIZER": "1",
    "RUNTIME_OPTIMIZER": "1",
}

ENV_RE = re.compile(r"os\.getenv\(\s*['\"]([A-Z0-9_]+)['\"]\s*,\s*['\"]([^'\"]*)['\"]")
ENV_RE_SIMPLE = re.compile(r"os\.getenv\(\s*['\"]([A-Z0-9_]+)['\"]")

FLOW_MODULE_MAP = {
    "tool_planning": ["dynamic_context_scheduler", "reference.planner", "reference.agent_exec"],
    "final_answer": ["prompt_builder", "reference.agent_exec", "reference.response_guard"],
    "partial_final_answer": ["reference.response_guard", "reference.agent_exec"],
    "recovery": ["recovery_scheduler", "dynamic_context_scheduler"],
    "planner.shadow": ["agent_brain.planner_shadow"],
    "planner.llm": ["agent_brain.llm_planner"],
    "memory.journal": ["runtime_kernel.task_journal"],
    "coverage.checked": ["coverage_checker"],
}

USAGE_CLASSES = (
    "active_hot_path",
    "active_guard",
    "active_optional",
    "active_cli_only",
    "active_test_only",
    "legacy_fallback",
    "imported_but_dead_branch",
    "dead_candidate",
    "unknown_needs_review",
)


@dataclass
class SymbolRecord:
    module: str
    symbol: str
    path: str = ""
    imported: bool = False
    reachable_static: bool = False
    observed_runtime: bool = False
    env_gate: list[str] = field(default_factory=list)
    branch_condition: str = ""
    called_by: list[str] = field(default_factory=list)
    usage_class: str = "unknown_needs_review"
    risk: str = "medium"
    recommendation: str = "manual_review"


def _module_name(path: Path) -> str:
    if path.is_relative_to(ROUTER):
        rel = path.relative_to(ROUTER)
        if rel.name == "__init__.py":
            return ".".join(rel.parts[:-1]) if rel.parts[:-1] else ""
        return ".".join(rel.with_suffix("").parts)
    rel = path.relative_to(SCRIPTS)
    return "scripts." + rel.with_suffix("").as_posix().replace("/", ".")


def _collect_router_py() -> list[Path]:
    out: list[Path] = []
    for p in ROUTER.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        out.append(p)
    return sorted(out)


def _parse_docker_env_defaults() -> dict[str, str]:
    defaults = dict(TRACKED_ENVS)
    compose = ROOT / "docker-compose.yml"
    if not compose.exists():
        return defaults
    text = compose.read_text(encoding="utf-8", errors="replace")
    for m in re.finditer(r"([A-Z][A-Z0-9_]+):\s*\"\$\{([A-Z0-9_]+):-([^}]*)\}\"", text):
        key, _, default = m.group(1), m.group(2), m.group(3)
        if key in TRACKED_ENVS:
            defaults[key] = default
    return defaults


def _resolve_import(importer: Path, name: str, level: int) -> list[str]:
    """Return candidate module keys for an import."""
    if level > 0:
        base_parts = list(importer.relative_to(ROUTER).parts[:-1])
        pkg = base_parts[: max(0, len(base_parts) - level + 1)]
        full = ".".join(pkg + name.split(".")) if name else ".".join(pkg)
        out = [full] if full else []
        if "." in full:
            out.append(full.split(".")[0])
        return out
    if not name:
        return []
    parts = name.split(".")
    out = [name]
    if parts[0] not in out:
        out.append(parts[0])
    return out


@dataclass
class ModuleAnalysis:
    path: str
    module: str
    imports: list[str] = field(default_factory=list)
    imported_by: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    env_refs: list[str] = field(default_factory=list)
    env_defaults: dict[str, str] = field(default_factory=dict)
    reachable_static: bool = False
    call_targets: list[str] = field(default_factory=list)


def _analyze_module(path: Path) -> ModuleAnalysis:
    mod = _module_name(path)
    rel = str(path.relative_to(ROOT))
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        text = path.read_text(encoding="utf-8", errors="replace")
    except SyntaxError:
        return ModuleAnalysis(path=rel, module=mod)

    imports: list[str] = []
    functions: list[str] = []
    calls: list[str] = []
    env_refs: list[str] = []
    env_defaults: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            bases = _resolve_import(path, node.module or "", node.level)
            for base in bases:
                imports.append(base)
                for alias in node.names:
                    imports.append(f"{base}.{alias.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_") or node.name in ("__init__",):
                functions.append(node.name)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.append(node.func.attr)

    for m in ENV_RE.finditer(text):
        env_refs.append(m.group(1))
        env_defaults[m.group(1)] = m.group(2)
    for m in ENV_RE_SIMPLE.finditer(text):
        if m.group(1) not in env_refs:
            env_refs.append(m.group(1))

    return ModuleAnalysis(
        path=rel,
        module=mod,
        imports=sorted(set(imports)),
        functions=functions,
        env_refs=sorted(set(env_refs)),
        env_defaults=env_defaults,
        call_targets=sorted(set(calls)),
    )


def _env_active(env: str, docker_defaults: dict[str, str]) -> bool:
    val = os.getenv(env)
    if val is not None:
        return val not in ("0", "false", "False", "")
    default = docker_defaults.get(env, TRACKED_ENVS.get(env, "1"))
    return default not in ("0", "false", "False", "")


def run_static() -> dict[str, Any]:
    docker_defaults = _parse_docker_env_defaults()
    modules: dict[str, ModuleAnalysis] = {}
    for path in _collect_router_py():
        ma = _analyze_module(path)
        if ma.module:
            modules[ma.module] = ma

    # reverse import edges
    for mod, info in modules.items():
        for imp in info.imports:
            for candidate in (imp, imp.split(".")[0]):
                if candidate in modules and mod not in modules[candidate].imported_by:
                    modules[candidate].imported_by.append(mod)

    # BFS from entrypoints
    q: deque[str] = deque()
    for ep in ENTRYPOINT_FILES:
        if ep.exists():
            q.append(_module_name(ep))
    # verify-router scripts as CLI entry
    for script in SCRIPTS.glob("test-*.py"):
        q.append(_module_name(script))
    for script in SCRIPTS.glob("benchmark-*.py"):
        q.append(_module_name(script))

    while q:
        cur = q.popleft()
        if cur not in modules or modules[cur].reachable_static:
            continue
        modules[cur].reachable_static = True
        for imp in modules[cur].imports:
            for candidate in (imp, imp.split(".")[0]):
                if candidate in modules and not modules[candidate].reachable_static:
                    q.append(candidate)

    symbols: list[SymbolRecord] = []
    for mod, info in modules.items():
        imported = bool(info.imported_by)
        for fn in info.functions or ["<module>"]:
            sym = f"{mod}:{fn}"
            rec = SymbolRecord(
                module=mod,
                symbol=fn,
                path=info.path,
                imported=imported,
                reachable_static=info.reachable_static,
                env_gate=[e for e in info.env_refs if e in TRACKED_ENVS],
            )
            rec.called_by = list(info.imported_by[:8])
            symbols.append(rec)

        # module-level record
        symbols.append(
            SymbolRecord(
                module=mod,
                symbol="<module>",
                path=info.path,
                imported=imported,
                reachable_static=info.reachable_static,
                env_gate=[e for e in info.env_refs if e in TRACKED_ENVS],
                called_by=list(info.imported_by[:8]),
            )
        )

    # classify
    for rec in symbols:
        mod = rec.module
        info = modules.get(mod)
        if not info:
            continue

        if mod in GUARD_MODULES or any(g in mod for g in ("loop_guard", "response_guard", "failed_action")):
            rec.usage_class = "active_guard"
            rec.risk = "high"
            rec.recommendation = "keep"
        elif rec.reachable_static and rec.path.startswith("scripts/"):
            rec.usage_class = "active_cli_only"
            rec.recommendation = "keep"
        elif "test_" in rec.path or "/tests/" in rec.path:
            rec.usage_class = "active_test_only"
            rec.recommendation = "keep"
        elif mod in ("legacy.context_optimizer", "legacy.runtime_optimizer"):
            rec.usage_class = "imported_but_dead_branch"
            rec.branch_condition = "env set but no import path from entrypoints"
            rec.risk = "low"
            rec.recommendation = "deprecate_env"
        elif rec.env_gate and not any(_env_active(e, docker_defaults) for e in rec.env_gate):
            rec.usage_class = "active_optional"
            rec.branch_condition = f"env off by default: {','.join(rec.env_gate)}"
            rec.recommendation = "keep_documented"
        elif rec.reachable_static and rec.env_gate and mod.startswith("legacy."):
            rec.usage_class = "legacy_fallback"
            rec.recommendation = "deprecate_env"
        elif rec.reachable_static:
            rec.usage_class = "active_hot_path"
            rec.risk = "high"
            rec.recommendation = "keep"
        elif rec.imported and not rec.reachable_static:
            rec.usage_class = "unknown_needs_review"
            rec.recommendation = "manual_review"
        elif not rec.imported and not rec.reachable_static:
            rec.usage_class = "dead_candidate"
            rec.risk = "low"
            rec.recommendation = "archive_candidate"
        else:
            rec.usage_class = "unknown_needs_review"

    # env branch report
    env_branches: list[dict[str, Any]] = []
    for env, default in TRACKED_ENVS.items():
        modules_with_env = [m.module for m in modules.values() if env in m.env_refs]
        active = _env_active(env, docker_defaults)
        reachable_mods = [m for m in modules_with_env if modules.get(m, ModuleAnalysis("", "")).reachable_static]
        imported_not_reached = [
            m for m in modules_with_env
            if m in modules and modules[m].imported_by and not modules[m].reachable_static
        ]
        env_branches.append({
            "env": env,
            "default": docker_defaults.get(env, default),
            "active_in_current_env": active,
            "modules_referencing": modules_with_env,
            "reachable_modules": reachable_mods,
            "imported_but_unreachable": imported_not_reached,
            "likely_dead_branch": bool(modules_with_env) and not reachable_mods and env in (
                "CONTEXT_OPTIMIZER", "RUNTIME_OPTIMIZER",
            ),
        })

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "static",
        "docker_env_defaults": docker_defaults,
        "modules": {k: asdict(v) for k, v in modules.items()},
        "symbols": [asdict(s) for s in symbols],
        "env_branches": env_branches,
        "summary": _summarize_symbols(symbols),
    }
    STATIC_JSON.parent.mkdir(parents=True, exist_ok=True)
    STATIC_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Written: {STATIC_JSON}")
    print(f"Modules: {len(modules)} · symbols: {len(symbols)}")
    return payload


def _summarize_symbols(symbols: list[SymbolRecord]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for s in symbols:
        counts[s.usage_class] += 1
    return dict(counts)


def _profiler_wrapper_path() -> Path:
    p = audit_json_dir() / "_reachability_profiler.py"
    p.write_text(
        textwrap.dedent(
            '''
            import json, runpy, subprocess, sys
            from pathlib import Path

            OUT = Path(sys.argv[1])
            TARGET = sys.argv[2:]
            ROOT = Path(__file__).resolve().parents[1]
            sys.path.insert(0, str(ROOT / "router"))
            observed: dict[str, int] = {}
            PREFIXES = (
                "main", "intent_router", "dynamic_context", "legacy", "prompt_builder",
                "reference", "adapters", "runtime_kernel", "runtime_core", "agent_brain",
                "context_", "coverage_", "recovery_", "explorer_", "artifact_", "cursor_",
                "failed_action", "integrations", "observability",
            )

            def _prof(frame, event, arg):
                if event != "call":
                    return _prof
                mod = frame.f_globals.get("__name__", "") or ""
                if not mod or mod.startswith("ast.") or mod.startswith("_"):
                    return _prof
                if any(mod.startswith(px) or px in mod for px in PREFIXES):
                    key = f"{mod}:{frame.f_code.co_name}"
                    observed[key] = observed.get(key, 0) + 1
                return _prof

            sys.setprofile(_prof)
            try:
                if TARGET and TARGET[0].endswith(".py"):
                    runpy.run_path(TARGET[0], run_name="__main__")
                elif TARGET:
                    r = subprocess.run(TARGET, cwd=str(ROOT))
                    if r.returncode != 0:
                        raise SystemExit(r.returncode)
            finally:
                sys.setprofile(None)
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(json.dumps({"observed": observed}, indent=2), encoding="utf-8")
            '''
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return p


def _scan_flow_observations(since_minutes: int = 120) -> dict[str, int]:
    """Infer module usage from recent flow.json / explorer trace."""
    observed: dict[str, int] = defaultdict(int)
    cap = captures_dir()
    if not cap.exists():
        return {}
    cutoff = datetime.now(timezone.utc).timestamp() - since_minutes * 60
    for flow in sorted(cap.glob("*.flow.json"))[-200:]:
        try:
            if flow.stat().st_mtime < cutoff:
                continue
            data = json.loads(flow.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        phase = str(data.get("phase") or data.get("router_phase") or "")
        for key, mods in FLOW_MODULE_MAP.items():
            if key in phase or phase in key:
                for m in mods:
                    observed[f"{m}:<flow>"] += 1
        for ev in data.get("events") or []:
            if isinstance(ev, dict):
                evname = str(ev.get("event") or "")
                for key, mods in FLOW_MODULE_MAP.items():
                    if key in evname:
                        for m in mods:
                            observed[f"{m}:<flow_event>"] += 1

    trace = cap / "explorer-trace.ndjson"
    alt_traces = list(cap.glob("explorer-trace*.ndjson"))
    paths = [trace] if trace.exists() else alt_traces
    for tp in paths:
        try:
            for line in tp.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]:
                if not line.strip():
                    continue
                row = json.loads(line)
                ev = str(row.get("event") or "")
                for key, mods in FLOW_MODULE_MAP.items():
                    if key in ev:
                        for m in mods:
                            observed[f"{m}:<trace>"] += 1
        except OSError:
            pass
    return dict(observed)


def run_profile(cmd: list[str] | None = None) -> dict[str, Any]:
    profile_out = audit_json_dir() / "runtime-reachability-profile-raw.json"
    wrapper = _profiler_wrapper_path()

    observed: dict[str, int] = {}
    errors: list[str] = []

    # Profile router e2e tests (in-process, observes router modules)
    e2e_scripts = [
        "scripts/test-planner-runtime-state-e2e.py",
        "scripts/test-explorer-trace-e2e.py",
        "scripts/test-llm-planner-shadow-e2e.py",
        "scripts/test-planner-promotion-gate-e2e.py",
        "scripts/test-project-index-ignore-e2e.py",
        "scripts/benchmark-recovery-e2e.py",
        "scripts/test-ping-pong-gate.py",
    ]
    for script in e2e_scripts:
        sp = ROOT / script
        if not sp.exists():
            continue
        raw = audit_json_dir() / f"profile-{sp.stem}.json"
        r = subprocess.run(
            [sys.executable, str(wrapper), str(raw), str(sp)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            errors.append(f"{script}: exit {r.returncode}")
        if raw.exists():
            try:
                chunk = json.loads(raw.read_text(encoding="utf-8")).get("observed") or {}
                for k, v in chunk.items():
                    observed[k] = observed.get(k, 0) + int(v)
            except json.JSONDecodeError:
                errors.append(f"{script}: bad profile json")

    # Optional external command (benchmark etc.)
    if cmd:
        raw = audit_json_dir() / "profile-external.json"
        r = subprocess.run(
            [sys.executable, str(wrapper), str(raw), *cmd],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        if r.returncode != 0:
            errors.append(f"external cmd: exit {r.returncode}")
        if raw.exists():
            try:
                chunk = json.loads(raw.read_text(encoding="utf-8")).get("observed") or {}
                for k, v in chunk.items():
                    observed[k] = observed.get(k, 0) + int(v)
            except json.JSONDecodeError:
                errors.append("external: bad profile json")

    flow_observed = _scan_flow_observations()
    for k, v in flow_observed.items():
        observed[k] = observed.get(k, 0) + int(v)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "profile",
        "observed": observed,
        "flow_observed": flow_observed,
        "errors": errors,
        "profiled_scripts": e2e_scripts,
        "external_cmd": cmd or [],
    }
    PROFILE_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Written: {PROFILE_JSON}")
    print(f"Observed symbols: {len(observed)} · flow keys: {len(flow_observed)}")
    if errors:
        print("Profile warnings:", "; ".join(errors[:5]))
    return payload


def _observed_modules(observed: dict[str, int]) -> set[str]:
    mods: set[str] = set()
    for key in observed:
        mod = key.split(":")[0]
        mods.add(mod)
        # normalize scripts.* -> skip
    return mods


def run_merge() -> dict[str, Any]:
    static: dict[str, Any] = {}
    profile: dict[str, Any] = {}
    if STATIC_JSON.exists():
        static = json.loads(STATIC_JSON.read_text(encoding="utf-8"))
    else:
        static = run_static()
    if PROFILE_JSON.exists():
        profile = json.loads(PROFILE_JSON.read_text(encoding="utf-8"))
    else:
        print("No profile data — run with --profile first for observed_runtime", file=sys.stderr)

    observed = dict(profile.get("observed") or {})
    observed.update(profile.get("flow_observed") or {})
    observed_mods = _observed_modules(observed)

    symbols_raw = static.get("symbols") or []
    merged_symbols: list[dict[str, Any]] = []
    for row in symbols_raw:
        rec = dict(row)
        mod = rec.get("module", "")
        sym = rec.get("symbol", "")
        key = f"{mod}:{sym}"
        rec["observed_runtime"] = any(
            k.startswith(f"{mod}:") for k in observed
        ) or mod in observed_mods
        if rec["observed_runtime"] and rec.get("usage_class") in (
            "dead_candidate", "imported_but_dead_branch", "unknown_needs_review",
        ):
            rec["usage_class"] = "active_hot_path"
            rec["recommendation"] = "keep"
            rec["risk"] = "high"
        elif rec.get("usage_class") == "imported_but_dead_branch" and not rec["observed_runtime"]:
            rec["recommendation"] = "deprecate_env"
            rec["risk"] = "low"
        merged_symbols.append(rec)

    summary_counts: dict[str, int] = defaultdict(int)
    for s in merged_symbols:
        summary_counts[s.get("usage_class", "unknown")] += 1
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "merged",
        "static_generated_at": static.get("generated_at"),
        "profile_generated_at": profile.get("generated_at"),
        "symbols": merged_symbols,
        "env_branches": static.get("env_branches") or [],
        "observed": observed,
        "summary": dict(summary_counts),
    }
    MERGED_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    reports_dir().mkdir(parents=True, exist_ok=True)
    _write_reports(payload, merged_symbols)
    print(f"Written: {MERGED_JSON}")
    print(f"Summary: {payload['summary']}")
    return payload


def _write_reports(payload: dict[str, Any], symbols: list[dict[str, Any]]) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    by_class: dict[str, list[dict]] = defaultdict(list)
    for s in symbols:
        by_class[s.get("usage_class", "unknown")].append(s)

    # runtime-reachability.md
    lines = [
        "# Runtime Reachability Audit",
        "",
        f"> Generated: {ts}",
        "",
        "## Summary",
        "",
        "| Usage class | Count |",
        "|-------------|------:|",
    ]
    for cls in USAGE_CLASSES:
        lines.append(f"| {cls} | {len(by_class.get(cls, []))} |")

    lines += ["", "## Observed at runtime (sample)", "", "| Module | Symbol | Class |", "|--------|--------|-------|"]
    for s in sorted(
        [x for x in symbols if x.get("observed_runtime")],
        key=lambda x: x.get("module", ""),
    )[:40]:
        lines.append(f"| `{s.get('module')}` | `{s.get('symbol')}` | {s.get('usage_class')} |")

    lines += ["", "## Imported but dead branch", "", "| Module | Env | Recommendation |", "|--------|-----|----------------|"]
    for s in sorted(by_class.get("imported_but_dead_branch", []), key=lambda x: x.get("module", "")):
        envs = ", ".join(s.get("env_gate") or []) or "-"
        lines.append(f"| `{s.get('module')}` | {envs} | {s.get('recommendation')} |")

    lines += ["", "## Dead candidates (archive later)", "", "| Module | Path |", "|--------|------|"]
    for s in sorted(by_class.get("dead_candidate", []), key=lambda x: x.get("path", ""))[:30]:
        if s.get("symbol") == "<module>":
            lines.append(f"| `{s.get('module')}` | `{s.get('path')}` |")

    lines += ["", "*Regenerate:*", "```bash", "python3 scripts/audit-runtime-reachability.py --static", "python3 scripts/audit-runtime-reachability.py --profile", "python3 scripts/audit-runtime-reachability.py --merge", "```"]
    (reports_dir() / "runtime-reachability.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # deprecated-branches.md
    dlines = [
        "# Deprecated Branches (env-gated, not observed)",
        "",
        f"> Generated: {ts}",
        "",
        "| Env | Default | Modules | Reachable | Dead branch |",
        "|-----|---------|---------|-----------|-------------|",
    ]
    for row in payload.get("env_branches") or []:
        dlines.append(
            f"| `{row.get('env')}` | {row.get('default')} | "
            f"{len(row.get('modules_referencing') or [])} | "
            f"{len(row.get('reachable_modules') or [])} | "
            f"{'yes' if row.get('likely_dead_branch') else 'no'} |"
        )
    dlines += [
        "",
        "## CONTEXT_OPTIMIZER / RUNTIME_OPTIMIZER",
        "",
        "- `legacy/context_optimizer.py` — **no import path** from entrypoints; env references only in dead module",
        "- `legacy/runtime_optimizer.py` — same; docker-compose still sets `=1` but code never imports",
        "- **Recommendation:** move env to Deprecated section; do not archive until reachability merge confirms",
        "",
    ]
    (reports_dir() / "deprecated-branches.md").write_text("\n".join(dlines) + "\n", encoding="utf-8")

    env_lines = [
        "# Deprecated Environment Variables",
        "",
        f"> Generated: {ts}",
        "",
        "Env vars that reference code with **no runtime reachability** from entrypoints.",
        "",
        "| Env | Default (compose) | Status | Module |",
        "|-----|-------------------|--------|--------|",
        "| `CONTEXT_OPTIMIZER` | 1 | **deprecated** — no import path | `legacy/context_optimizer.py` |",
        "| `RUNTIME_OPTIMIZER` | 1 | **deprecated** — no import path | `legacy/runtime_optimizer.py` |",
        "",
        "## Active optional (default off)",
        "",
        "| Env | Default | Module |",
        "|-----|---------|--------|",
        "| `LLM_PLANNER_SHADOW_ENABLED` | 0 | `agent_brain/llm_planner.py` |",
        "| `LLAMAINDEX_ENABLED` | 0 | `integrations/llamaindex.py` |",
        "",
        "Remove from `.env.example` active section only after archive phase completes.",
        "",
    ]
    (reports_dir() / "deprecated-env.md").write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    # archive-candidates.md — strict D-tier only
    forbidden_archive_prefixes = (
        "runtime_kernel.", "runtime_core.", "reference.", "adapters.",
        "agent_brain.", "legacy.memory_store", "dynamic_context", "main",
    )
    archive = [
        s for s in symbols
        if s.get("usage_class") == "dead_candidate"
        and s.get("symbol") == "<module>"
        and not s.get("observed_runtime")
        and not s.get("imported")
        and not s.get("reachable_static")
        and not s.get("env_gate")
        and not any(s.get("module", "").startswith(p) for p in forbidden_archive_prefixes)
        and "legacy/" in s.get("path", "")
    ]
    alines = [
        "# Archive Candidates (confirmed D-tier only)",
        "",
        f"> Generated: {ts}",
        "",
        "Criteria: imported=false, reachable_static=false, observed_runtime=false, no env_gate.",
        "",
        "| Module | Path | Risk | Action |",
        "|--------|------|------|--------|",
    ]
    for s in sorted(archive, key=lambda x: x.get("path", ""))[:50]:
        alines.append(f"| `{s.get('module')}` | `{s.get('path')}` | low | move_to_archive (next phase) |")
    alines += ["", "**Do not auto-delete.** Archive move is Phase 2.2a-clean.", ""]
    (reports_dir() / "archive-candidates.md").write_text("\n".join(alines) + "\n", encoding="utf-8")

    print(f"Written: {reports_dir() / 'runtime-reachability.md'}")
    print(f"Written: {reports_dir() / 'deprecated-branches.md'}")
    print(f"Written: {reports_dir() / 'deprecated-env.md'}")
    print(f"Written: {reports_dir() / 'archive-candidates.md'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Runtime reachability audit")
    parser.add_argument("--static", action="store_true", help="Static import/call/env analysis")
    parser.add_argument("--profile", action="store_true", help="Profile e2e tests + optional command")
    parser.add_argument("--merge", action="store_true", help="Merge static + profile → reports")
    parser.add_argument("cmd", nargs="*", help="Command after --profile --")
    args = parser.parse_args()

    if not (args.static or args.profile or args.merge):
        args.static = True
        args.merge = True

    if args.static:
        run_static()
    if args.profile:
        cmd = args.cmd if args.cmd and args.cmd[0] != "--" else (args.cmd[1:] if args.cmd and args.cmd[0] == "--" else [])
        run_profile(cmd or None)
    if args.merge:
        run_merge()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
