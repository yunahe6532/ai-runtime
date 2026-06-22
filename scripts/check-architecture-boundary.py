#!/usr/bin/env python3
"""Architecture boundary checker — import-layer rules for router/."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "router"
SCRIPTS = ROOT / "scripts"

# relpath -> allowed legacy import module prefixes (empty = none)
LEGACY_IMPORT_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "router/prompt_builder.py": ("legacy.retriever",),
}

# Paths that may import legacy (prefix match)
LEGACY_IMPORTER_PREFIXES = (
    "router/adapters/",
    "router/integrations/",
    "router/legacy/",
)

# Scripts allowed to import legacy directly (unit tests of legacy backends)
LEGACY_IMPORTER_SCRIPTS = (
    "scripts/test-memory-store.py",
    "scripts/test-context-optimizer.py",
)

# runtime_core must not import these top-level module roots
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

# reference/ must not import these roots
REFERENCE_FORBIDDEN_ROOTS = frozenset({"legacy", "integrations"})

# Orchestration (router flat app layer) must not import legacy except allowlist
ORCHESTRATION_FORBIDDEN_LEGACY = True

ORCHESTRATION_SKIP = frozenset(
    {
        "router/runtime_core",
        "router/adapters",
        "router/legacy",
        "router/integrations",
        "router/reference",
    }
)


@dataclass
class Violation:
    file: str
    line: int
    rule: str
    detail: str


def _rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _imports(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    out: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.append((node.lineno, node.module))
    return out


def _root(module: str) -> str:
    return module.split(".", 1)[0]


def _is_orchestration(rel: str) -> bool:
    if not rel.startswith("router/") or not rel.endswith(".py"):
        return False
    for skip in ORCHESTRATION_SKIP:
        if rel.startswith(skip + "/") or rel == skip + ".py":
            return False
    if rel.startswith("router/legacy/"):
        return False
    return True


def _legacy_import_allowed(rel: str, module: str) -> bool:
    if _root(module) != "legacy" and not module.startswith("legacy."):
        return True
    allowed = LEGACY_IMPORT_ALLOWLIST.get(rel, ())
    if any(module == p or module.startswith(p + ".") for p in allowed):
        return True
    if any(rel.startswith(p) for p in LEGACY_IMPORTER_PREFIXES):
        return True
    if rel in LEGACY_IMPORTER_SCRIPTS:
        return True
    return False


def check_file(path: Path) -> list[Violation]:
    rel = _rel(path)
    violations: list[Violation] = []
    for lineno, module in _imports(path):
        root = _root(module)

        if rel.startswith("router/runtime_core/") and root in RUNTIME_CORE_FORBIDDEN_ROOTS:
            violations.append(
                Violation(rel, lineno, "runtime_core_isolation", f"forbidden import: {module}")
            )

        if rel.startswith("router/reference/") and root in REFERENCE_FORBIDDEN_ROOTS:
            violations.append(
                Violation(rel, lineno, "reference_isolation", f"forbidden import: {module}")
            )

        if _is_orchestration(rel) and ORCHESTRATION_FORBIDDEN_LEGACY:
            if root == "legacy" or module.startswith("legacy."):
                if not _legacy_import_allowed(rel, module):
                    violations.append(
                        Violation(rel, lineno, "orchestration_no_legacy", f"legacy import: {module}")
                    )

        if root == "legacy" or module.startswith("legacy."):
            if not _legacy_import_allowed(rel, module):
                violations.append(
                    Violation(rel, lineno, "legacy_private", f"legacy import not allowed here: {module}")
                )

        # Deprecated top-level shims must not exist
        for banned in ("memory_store", "retriever", "agent_runs", "flow_trace"):
            if module == banned or module.startswith(banned + "."):
                violations.append(
                    Violation(rel, lineno, "removed_shim", f"use adapters/ or legacy/: {module}")
                )

    return violations


def collect_py_files() -> list[Path]:
    files: list[Path] = []
    for base in (ROUTER, SCRIPTS):
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return sorted(files)


def run_check() -> tuple[int, list[Violation]]:
    violations: list[Violation] = []
    for path in collect_py_files():
        try:
            violations.extend(check_file(path))
        except SyntaxError as exc:
            violations.append(
                Violation(_rel(path), exc.lineno or 0, "syntax_error", str(exc))
            )

    # Top-level shim files must not exist
    for name in ("memory_store.py", "retriever.py", "agent_runs.py", "flow_trace.py"):
        shim = ROUTER / name
        if shim.exists():
            violations.append(
                Violation(_rel(shim), 0, "shim_exists", f"remove top-level shim {name}")
            )

    return len(violations), violations


def main() -> int:
    count, violations = run_check()
    if not violations:
        print("architecture boundary: OK (0 violations)")
        return 0

    print(f"architecture boundary: FAIL ({count} violations)\n")
    by_rule: dict[str, list[Violation]] = {}
    for v in violations:
        by_rule.setdefault(v.rule, []).append(v)

    for rule, items in sorted(by_rule.items()):
        print(f"[{rule}]")
        for v in items:
            loc = f"{v.file}:{v.line}" if v.line else v.file
            print(f"  {loc}  {v.detail}")
        print()

    return 1


if __name__ == "__main__":
    sys.exit(main())
