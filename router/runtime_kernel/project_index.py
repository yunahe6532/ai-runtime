"""Project Index Bootstrap — structure map with invalidation fingerprints.

Runtime Memory (cold). LLM does not hold project structure; index does.
Bootstrap runs via tool pipeline (find/glob), not LLM inference.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

LOG = logging.getLogger("runtime_kernel.project_index")

INDEX_VERSION = 2

DEFAULT_INCLUDE_DIRS = frozenset({
    "router", "runtime_kernel", "runtime_core", "agent_brain", "adapters",
    "reference", "legacy", "observability", "integrations", "docs", "scripts",
    "config", "configs", "tests", "ui",
})

DEFAULT_EXCLUDE_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".pytest_cache", ".venv", ".venv-llamaindex",
    ".venv-llamaindex", "tmp", "captures", "dist", "build", "coverage",
    ".cursor", ".codex", ".runtime-index", ".project-index", "langfuse-data",
    "benchmarks", "benchmark-results",
})

DEFAULT_EXCLUDE_GLOBS = (
    "*.ndjson", "*.log", "*.trace", "*.pyc", "*.pyo",
    "docs/FILE_TREE.md", "docs/reports/FILE_TREE.full.md",
)

SCAN_EXTENSIONS = frozenset({".py", ".md", ".yaml", ".yml", ".json", ".toml", ".sh"})


class PathClass(str, Enum):
    SOURCE = "source"
    DOC = "doc"
    CONFIG = "config"
    TEST = "test"
    SCRIPT = "script"
    VENDOR = "vendor"
    GENERATED = "generated"
    RUNTIME_DATA = "runtime_data"
    CACHE = "cache"
    GIT_METADATA = "git_metadata"
    UNKNOWN = "unknown"


@dataclass
class ProjectIndexConfig:
    include_dirs: frozenset[str] = field(default_factory=lambda: DEFAULT_INCLUDE_DIRS)
    exclude_dirs: frozenset[str] = field(default_factory=lambda: DEFAULT_EXCLUDE_DIRS)
    exclude_globs: tuple[str, ...] = DEFAULT_EXCLUDE_GLOBS
    max_file_size: int = 2_000_000
    max_files_per_dir: int = 500
    max_files: int = 500

    def to_dict(self) -> dict[str, Any]:
        return {
            "include_dirs": sorted(self.include_dirs),
            "exclude_dirs": sorted(self.exclude_dirs),
            "exclude_globs": list(self.exclude_globs),
            "max_file_size": self.max_file_size,
            "max_files_per_dir": self.max_files_per_dir,
            "max_files": self.max_files,
        }


def _normalize_relpath(path: str | Path) -> str:
    p = str(path).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def classify_path(path: str | Path, *, workspace: str | Path | None = None) -> PathClass:
    """Classify a repo-relative or absolute path for index inclusion policy."""
    p = _normalize_relpath(path)
    parts = [x for x in p.split("/") if x]
    if not parts:
        return PathClass.UNKNOWN

    if parts[0] == ".git" or ".git" in parts:
        return PathClass.GIT_METADATA

    lower_parts = {x.lower() for x in parts}
    if lower_parts & {"node_modules", ".venv", ".venv-llamaindex"}:
        return PathClass.VENDOR
    if "tmp" in lower_parts or "captures" in lower_parts:
        return PathClass.RUNTIME_DATA
    if "__pycache__" in lower_parts or ".pytest_cache" in lower_parts:
        return PathClass.CACHE
    if parts[0] in ("dist", "build", "coverage", "benchmark-results", "benchmarks"):
        return PathClass.GENERATED

    name = parts[-1]
    if name in ("FILE_TREE.md", "FILE_TREE.full.md") or "FILE_TREE" in name:
        return PathClass.GENERATED
    for pat in DEFAULT_EXCLUDE_GLOBS:
        if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(name, pat):
            return PathClass.GENERATED

    if name.startswith("test_") or "/tests/" in f"/{p}/" or parts[0] == "tests":
        return PathClass.TEST
    if parts[0] in ("scripts",):
        return PathClass.SCRIPT
    if parts[0] in ("docs",) or name.endswith(".md"):
        return PathClass.DOC
    if parts[0] in ("config", "configs") or name in (
        "docker-compose.yml", "pyproject.toml", ".gitignore", ".dockerignore",
    ):
        return PathClass.CONFIG
    if name.endswith(".py") and parts[0] in DEFAULT_INCLUDE_DIRS:
        return PathClass.SOURCE
    if parts[0] in DEFAULT_INCLUDE_DIRS:
        return PathClass.SOURCE
    return PathClass.UNKNOWN


def path_included_in_index(relpath: str, cfg: ProjectIndexConfig | None = None) -> bool:
    pc = classify_path(relpath)
    return pc in {
        PathClass.SOURCE, PathClass.DOC, PathClass.CONFIG,
        PathClass.TEST, PathClass.SCRIPT,
    }


def _should_skip_dir(name: str, cfg: ProjectIndexConfig) -> bool:
    if name in cfg.exclude_dirs:
        return True
    if name.startswith(".") and name not in {".github"}:
        return True
    return False


def _matches_exclude_glob(relpath: str, cfg: ProjectIndexConfig) -> bool:
    for pat in cfg.exclude_globs:
        if fnmatch.fnmatch(relpath, pat) or fnmatch.fnmatch(Path(relpath).name, pat):
            return True
    return False


@dataclass
class FileEntry:
    path: str
    relpath: str
    size: int
    mtime: float
    content_hash: str
    path_class: str = "source"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProjectIndex:
    project_key: str
    workspace: str
    index_version: int = INDEX_VERSION
    project_fingerprint: str = ""
    git_commit: str = ""
    built_at: str = ""
    file_count: int = 0
    dir_tree: list[str] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    symbol_hints: list[str] = field(default_factory=list)
    excluded_summary: dict[str, Any] = field(default_factory=dict)
    index_config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ProjectIndex | None:
        if not data:
            return None
        fields = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**fields)


def _file_hash(path: Path, max_bytes: int = 65536) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            h.update(f.read(max_bytes))
        h.update(str(path.stat().st_size).encode())
    except OSError:
        return ""
    return h.hexdigest()[:16]


def _git_head(workspace: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", workspace, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if r.returncode == 0:
            return (r.stdout or "").strip()[:12]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _scan_workspace(
    workspace: str,
    *,
    cfg: ProjectIndexConfig | None = None,
) -> tuple[list[FileEntry], list[str], dict[str, Any]]:
    config = cfg or ProjectIndexConfig()
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        return [], [], {}

    files: list[FileEntry] = []
    dirs_seen: set[str] = set()
    excluded: dict[str, dict[str, Any]] = {}
    per_dir_counts: dict[str, int] = {}

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d, config)]
        rel_dir = str(Path(dirpath).relative_to(root))
        if rel_dir != ".":
            dirs_seen.add(rel_dir.replace("\\", "/"))
            top = rel_dir.split("/")[0].replace("\\", "/")
            if top in config.exclude_dirs or classify_path(rel_dir) in (
                PathClass.VENDOR, PathClass.RUNTIME_DATA, PathClass.CACHE, PathClass.GENERATED,
            ):
                key = top
                excluded.setdefault(key, {"reason": classify_path(rel_dir).value, "files": 0})
                excluded[key]["files"] += len(filenames)
                dirnames.clear()
                continue

        for name in filenames:
            if len(files) >= config.max_files:
                break
            p = Path(dirpath) / name
            rel = str(p.relative_to(root)).replace("\\", "/")
            pc = classify_path(rel)

            if not path_included_in_index(rel, config):
                top = rel.split("/")[0]
                excluded.setdefault(top, {"reason": pc.value, "files": 0})
                excluded[top]["files"] += 1
                continue

            if p.suffix.lower() not in SCAN_EXTENSIONS:
                continue
            if _matches_exclude_glob(rel, config):
                excluded.setdefault("globs", {"reason": "exclude_glob", "files": 0})
                excluded["globs"]["files"] += 1
                continue

            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > config.max_file_size:
                excluded.setdefault("oversized", {"reason": "max_file_size", "files": 0})
                excluded["oversized"]["files"] += 1
                continue

            parent = str(Path(rel).parent)
            per_dir_counts[parent] = per_dir_counts.get(parent, 0) + 1
            if per_dir_counts[parent] > config.max_files_per_dir:
                continue

            files.append(
                FileEntry(
                    path=str(p),
                    relpath=rel,
                    size=int(st.st_size),
                    mtime=float(st.st_mtime),
                    content_hash=_file_hash(p),
                    path_class=pc.value,
                )
            )

    return files, sorted(dirs_seen)[:200], excluded


def _fingerprint(files: list[FileEntry], git_commit: str) -> str:
    blob = "|".join(f"{f.relpath}:{f.content_hash}:{int(f.mtime)}" for f in sorted(files, key=lambda x: x.relpath))
    blob = f"{git_commit}|{blob}"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _detect_entrypoints(files: list[FileEntry]) -> list[str]:
    names = {f.relpath for f in files}
    hints: list[str] = []
    for candidate in (
        "main.py", "router/main.py", "app.py", "index.ts", "package.json",
        "docker-compose.yml", "README.md", "pyproject.toml",
    ):
        if candidate in names:
            hints.append(candidate)
    return hints[:12]


def bootstrap_project_index(
    workspace: str,
    project_key: str = "",
    *,
    cfg: ProjectIndexConfig | None = None,
) -> ProjectIndex:
    """One-time (or stale) project scan — shell/filesystem, not LLM."""
    config = cfg or ProjectIndexConfig()
    ws = str(Path(workspace).expanduser().resolve()) if workspace else ""
    pk = project_key or hashlib.sha256(ws.encode()).hexdigest()[:12] if ws else "unknown"
    git_commit = _git_head(ws) if ws else ""
    file_entries, dir_tree, excluded = _scan_workspace(ws, cfg=config) if ws else ([], [], {})
    fp = _fingerprint(file_entries, git_commit)
    entrypoints = _detect_entrypoints(file_entries)
    symbol_hints = [
        f.relpath for f in file_entries
        if f.relpath.endswith(".py") and any(x in f.relpath for x in ("router/", "runtime_kernel/", "reference/"))
    ][:40]
    idx = ProjectIndex(
        project_key=pk,
        workspace=ws,
        project_fingerprint=fp,
        git_commit=git_commit,
        built_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        file_count=len(file_entries),
        dir_tree=dir_tree,
        files=[f.to_dict() for f in file_entries],
        entrypoints=entrypoints,
        symbol_hints=symbol_hints,
        excluded_summary=excluded,
        index_config=config.to_dict(),
    )
    LOG.info(
        "project_index_bootstrap pk=%s files=%d dirs=%d excluded_keys=%d fingerprint=%s git=%s",
        pk, len(file_entries), len(dir_tree), len(excluded), fp, git_commit or "-",
    )
    return idx


def index_is_stale(stored: ProjectIndex | dict[str, Any] | None, workspace: str) -> bool:
    if not stored:
        return True
    idx = stored if isinstance(stored, ProjectIndex) else ProjectIndex.from_dict(stored)
    if not idx:
        return True
    if idx.index_version != INDEX_VERSION:
        return True
    ws = str(Path(workspace).expanduser().resolve()) if workspace else ""
    if ws and idx.workspace and Path(ws).resolve() != Path(idx.workspace).resolve():
        return True
    current_git = _git_head(ws) if ws else ""
    if current_git and idx.git_commit and current_git != idx.git_commit:
        return True
    if ws:
        sample, _, _ = _scan_workspace(ws, cfg=ProjectIndexConfig())
        current_fp = _fingerprint(sample, current_git)
        if idx.project_fingerprint and current_fp != idx.project_fingerprint:
            return True
    return False


def index_path(project_key: str, base: Path | None = None) -> Path:
    from legacy.memory_store import PROJECTS_DIR

    root = base or PROJECTS_DIR
    return root / project_key / "index" / "project_index.json"


def load_project_index(project_key: str) -> ProjectIndex | None:
    p = index_path(project_key)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return ProjectIndex.from_dict(data)
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def save_project_index(index: ProjectIndex) -> None:
    p = index_path(index.project_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(index.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_project_index(state: Any, workspace: str = "") -> ProjectIndex | None:
    """Load or bootstrap project index into session state."""
    ws = workspace or getattr(state, "effective_workspace", "") or getattr(state, "workspace_path", "")
    pk = getattr(state, "project_key", "") or "unknown"
    stored_raw = getattr(state, "project_index", None) or {}
    stored = ProjectIndex.from_dict(stored_raw) if stored_raw else load_project_index(pk)

    if index_is_stale(stored, ws):
        if ws and Path(ws).is_dir():
            idx = bootstrap_project_index(ws, pk)
            save_project_index(idx)
            state.project_index = idx.to_dict()
            return idx
        return stored

    if stored and not getattr(state, "project_index", None):
        state.project_index = stored.to_dict()
    return stored
