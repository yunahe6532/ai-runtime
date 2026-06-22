"""Project Index Bootstrap — structure map with invalidation fingerprints.

Runtime Memory (cold). LLM does not hold project structure; index does.
Bootstrap runs via tool pipeline (find/glob), not LLM inference.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

LOG = logging.getLogger("runtime_kernel.project_index")

INDEX_VERSION = 1
SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "dist", "build",
    ".codex", "tmp", ".cursor", "ui/node_modules",
})
SCAN_EXTENSIONS = frozenset({".py", ".md", ".yaml", ".yml", ".json", ".toml", ".sh"})


@dataclass
class FileEntry:
    path: str
    relpath: str
    size: int
    mtime: float
    content_hash: str

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


def _scan_workspace(workspace: str, *, max_files: int = 500) -> tuple[list[FileEntry], list[str]]:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        return [], []
    files: list[FileEntry] = []
    dirs_seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        rel_dir = str(Path(dirpath).relative_to(root))
        if rel_dir != ".":
            dirs_seen.add(rel_dir.replace("\\", "/"))
        for name in filenames:
            if len(files) >= max_files:
                break
            p = Path(dirpath) / name
            if p.suffix.lower() not in SCAN_EXTENSIONS:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            rel = str(p.relative_to(root)).replace("\\", "/")
            files.append(
                FileEntry(
                    path=str(p),
                    relpath=rel,
                    size=int(st.st_size),
                    mtime=float(st.st_mtime),
                    content_hash=_file_hash(p),
                )
            )
    return files, sorted(dirs_seen)[:200]


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


def bootstrap_project_index(workspace: str, project_key: str = "") -> ProjectIndex:
    """One-time (or stale) project scan — shell/filesystem, not LLM."""
    ws = str(Path(workspace).expanduser().resolve()) if workspace else ""
    pk = project_key or hashlib.sha256(ws.encode()).hexdigest()[:12] if ws else "unknown"
    git_commit = _git_head(ws) if ws else ""
    file_entries, dir_tree = _scan_workspace(ws) if ws else ([], [])
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
    )
    LOG.info(
        "project_index_bootstrap pk=%s files=%d dirs=%d fingerprint=%s git=%s",
        pk, len(file_entries), len(dir_tree), fp, git_commit or "-",
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
        sample, _ = _scan_workspace(ws, max_files=32)
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
