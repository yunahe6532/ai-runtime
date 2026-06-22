#!/usr/bin/env python3
"""Dead code / legacy usage audit — import graph from entrypoints, no auto-delete."""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))

from runtime_kernel.runtime_paths import audit_json_dir, repo_root, reports_dir  # noqa: E402

REPO = repo_root()
ROUTER = REPO / "router"
SCRIPTS = REPO / "scripts"

ENTRYPOINTS = [
    ROUTER / "main.py",
    ROUTER / "dynamic_context_scheduler.py",
    ROUTER / "legacy" / "memory_store.py",
    ROUTER / "reference" / "agent_exec.py",
    ROUTER / "reference" / "response_guard.py",
]

CLASSIFICATIONS = (
    "active_hot_path",
    "active_test_only",
    "active_cli_only",
    "legacy_fallback",
    "planned",
    "dead_candidate",
    "unknown_needs_review",
)

LEGACY_CANDIDATES = [
    "router/legacy/retriever.py",
    "router/legacy/agent_runs.py",
]

ARCHIVED_LEGACY = [
    "router/legacy/context_optimizer.py",
    "router/legacy/runtime_optimizer.py",
]

ENV_RE = re.compile(r"os\.getenv\(\s*['\"]([A-Z0-9_]+)['\"]")


@dataclass
class ModuleInfo:
    path: str
    module: str
    imports: list[str] = field(default_factory=list)
    imported_by: list[str] = field(default_factory=list)
    reachable: bool = False
    classification: str = "unknown_needs_review"
    env_refs: list[str] = field(default_factory=list)
    mtime: float = 0.0
    size: int = 0


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROUTER)
    if rel.name == "__init__.py":
        return ".".join(rel.parts[:-1])
    return ".".join(rel.with_suffix("").parts)


def _resolve_import(importer: Path, name: str, level: int) -> str | None:
    if level > 0:
        base_parts = list(importer.relative_to(ROUTER).parts[:-1])
        if level > len(base_parts) + 1:
            return None
        pkg = base_parts[: len(base_parts) - level + 1] if level <= len(base_parts) else []
        full = ".".join(pkg + name.split(".")) if name else ".".join(pkg)
        return full or None
    return name.split(".")[0] if name else None


def _path_for_module(mod: str) -> Path | None:
    parts = mod.split(".")
    candidates = [
        ROUTER / Path(*parts).with_suffix(".py"),
        ROUTER / Path(*parts) / "__init__.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _collect_py_files() -> list[Path]:
    files: list[Path] = []
    for base in (ROUTER, SCRIPTS):
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            rel = str(p.relative_to(REPO))
            if any(x in rel for x in ("node_modules", "__pycache__", ".venv")):
                continue
            files.append(p)
    return files


def _parse_imports(path: Path) -> tuple[list[str], list[str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return [], []
    imports: list[str] = []
    envs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_import(path, node.module or "", node.level)
            if base:
                imports.append(base)
                for alias in node.names:
                    imports.append(f"{base}.{alias.name}")
    text = path.read_text(encoding="utf-8", errors="replace")
    envs = sorted(set(ENV_RE.findall(text)))
    return imports, envs


def build_graph() -> dict[str, ModuleInfo]:
    modules: dict[str, ModuleInfo] = {}
    for path in _collect_py_files():
        if path.is_relative_to(ROUTER):
            mod = _module_name(path)
        else:
            mod = "scripts." + path.relative_to(SCRIPTS).with_suffix("").as_posix().replace("/", ".")
        imps, envs = _parse_imports(path)
        try:
            st = path.stat()
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            mtime, size = 0.0, 0
        modules[mod] = ModuleInfo(
            path=str(path.relative_to(REPO)),
            module=mod,
            imports=imps,
            env_refs=envs,
            mtime=mtime,
            size=size,
        )

    # reverse edges
    for mod, info in modules.items():
        for imp in info.imports:
            root_imp = imp.split(".")[0]
            for candidate in (imp, root_imp):
                if candidate in modules and candidate != mod:
                    if mod not in modules[candidate].imported_by:
                        modules[candidate].imported_by.append(mod)

    # BFS from entrypoints
    q: deque[str] = deque()
    for ep in ENTRYPOINTS:
        if ep.exists():
            q.append(_module_name(ep))
    while q:
        cur = q.popleft()
        if cur not in modules or modules[cur].reachable:
            continue
        modules[cur].reachable = True
        for imp in modules[cur].imports:
            for candidate in (imp, imp.split(".")[0]):
                if candidate in modules and not modules[candidate].reachable:
                    q.append(candidate)

    test_only_prefixes = ("scripts.test_", "test_")
    for mod, info in modules.items():
        if info.reachable:
            if info.path.startswith("scripts/"):
                info.classification = "active_cli_only"
            elif "/tests/" in info.path or "test_" in Path(info.path).name:
                info.classification = "active_test_only"
            elif "legacy/" in info.path:
                info.classification = "legacy_fallback"
            else:
                info.classification = "active_hot_path"
        elif info.path in LEGACY_CANDIDATES or "legacy/" in info.path:
            info.classification = "legacy_fallback"
        elif info.imported_by:
            info.classification = "unknown_needs_review"
        else:
            info.classification = "dead_candidate"
    return modules


def render_md(modules: dict[str, ModuleInfo]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    by_class: dict[str, list[ModuleInfo]] = defaultdict(list)
    for m in modules.values():
        by_class[m.classification].append(m)

    lines = [
        "# Dead Code Audit",
        "",
        f"> Generated: {ts}",
        f"> Entrypoints: {len(ENTRYPOINTS)}",
        "",
        "## Summary",
        "",
        "| Classification | Modules |",
        "|----------------|--------:|",
    ]
    for cls in CLASSIFICATIONS:
        lines.append(f"| {cls} | {len(by_class.get(cls, []))} |")

    lines += ["", "## Dead candidates (sample)", "", "| Module | Path | Imported by |", "|--------|------|-------------|"]
    for m in sorted(by_class.get("dead_candidate", []), key=lambda x: x.path)[:40]:
        refs = ", ".join(m.imported_by[:3]) or "-"
        lines.append(f"| `{m.module}` | `{m.path}` | {refs} |")

    lines += ["", "## Legacy fallback", "", "| Module | Env refs | Reachable |", "|--------|----------|-----------|"]
    for m in sorted(by_class.get("legacy_fallback", []), key=lambda x: x.path):
        envs = ", ".join(m.env_refs[:4]) or "-"
        lines.append(f"| `{m.path}` | {envs} | {m.reachable} |")

    lines += ["", "## Unknown (needs review)", ""]
    for m in sorted(by_class.get("unknown_needs_review", []), key=lambda x: x.path)[:30]:
        lines.append(f"- `{m.path}` ← {', '.join(m.imported_by[:4]) or 'none'}")

    lines += [
        "",
        "## Known legacy archive status",
        "",
        "| File | Status |",
        "|------|--------|",
        "| `legacy/context_optimizer.py` | **archived** 2026-06-22 (stub in repo) |",
        "| `legacy/runtime_optimizer.py` | **archived** 2026-06-22 (stub in repo) |",
        "| thin adapter wrappers | verify adapters/* usage |",
        "| old explorer/read_only code | superseded by read_only_explorer |",
        "",
        "*Regenerate: `python3 scripts/audit-dead-code.py`*",
        "",
        "See also: `docs/reports/legacy-archive-plan.md`",
    ]
    return "\n".join(lines) + "\n"


def render_archive_plan(modules: dict[str, ModuleInfo]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# Legacy Archive Plan",
        "",
        f"> Generated: {ts}",
        "",
        "| File | Referenced | Last modified (mtime) | Related env | Risk | Action |",
        "|------|------------|---------------------|-------------|------|--------|",
    ]
    candidates = [
        m for m in modules.values()
        if m.classification in ("dead_candidate", "legacy_fallback", "unknown_needs_review")
        and ("legacy/" in m.path or m.path in LEGACY_CANDIDATES)
    ]
    for m in sorted(candidates, key=lambda x: x.path):
        risk = "low" if m.classification == "dead_candidate" and not m.imported_by else "medium"
        if m.reachable and "legacy/" in m.path:
            risk = "high"
        action = {
            "dead_candidate": "move_to_archive",
            "legacy_fallback": "deprecate_env",
            "unknown_needs_review": "manual_review",
        }.get(m.classification, "manual_review")
        if m.reachable:
            action = "keep"
        mtime_s = datetime.fromtimestamp(m.mtime, tz=timezone.utc).strftime("%Y-%m-%d") if m.mtime else "-"
        refs = "yes" if m.imported_by or m.reachable else "no"
        envs = ", ".join(m.env_refs[:3]) or "-"
        lines.append(f"| `{m.path}` | {refs} | {mtime_s} | {envs} | {risk} | {action} |")

    extras = [
        ("duplicate extract_recent_agent_tail", "prompt_builder / planner", "manual_review"),
        ("duplicate extract_original_system", "prompt_builder / context_need", "manual_review"),
        ("old build_context_pack path", "legacy optimizer", "deprecate_env"),
    ]
    for name, note, action in extras:
        lines.append(f"| {name} | ? | - | - | medium | {action} |")

    lines += [
        "",
        "## Action glossary",
        "",
        "- **keep** — active or reachable fallback",
        "- **move_to_archive** — unreachable, no importers",
        "- **deprecate_env** — gated by env, document before removal",
        "- **merge** — duplicate logic consolidation",
        "- **delete_after_tests** — remove only after coverage proof",
        "- **manual_review** — human decision required",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    modules = build_graph()
    md = render_md(modules)
    archive = render_archive_plan(modules)
    reports_dir().mkdir(parents=True, exist_ok=True)
    md_path = reports_dir() / "dead-code-audit.md"
    archive_path = reports_dir() / "legacy-archive-plan.md"
    json_path = audit_json_dir() / "dead-code-audit.json"
    md_path.write_text(md, encoding="utf-8")
    archive_path.write_text(archive, encoding="utf-8")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "modules": {k: asdict(v) for k, v in modules.items()},
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    dead = sum(1 for m in modules.values() if m.classification == "dead_candidate")
    print(f"Written: {md_path}")
    print(f"Written: {archive_path}")
    print(f"Written: {json_path}")
    print(f"Modules: {len(modules)} · dead_candidate: {dead}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
