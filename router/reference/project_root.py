"""Resolve repository root — never treat a bare home directory as project root."""

from __future__ import annotations

import os
from pathlib import Path

REPO_MARKERS = (".git", "docker-compose.yml", "pyproject.toml")
REPO_DIR_MARKERS = ("router",)

# Container layout: /app = router package; repo may be parent on host mounts.
_ROUTER_DIR = Path(__file__).resolve().parents[1]


def _is_repo_root(path: Path) -> bool:
    if not path.is_dir():
        return False
    if not (path / "router").is_dir():
        return False
    return any((path / m).exists() for m in REPO_MARKERS) or (path / "router" / "main.py").is_file()


def is_router_package_root(path: Path) -> bool:
    """True when path is the router package itself (e.g. Docker /app), not full repo."""
    if not path.is_dir():
        return False
    if _is_repo_root(path):
        return False
    return (path / "runtime_core").is_dir() and (path / "main.py").is_file()


def is_container_router_path(path: str) -> bool:
    """Detect router-in-container workspace hints that must not be used as host root."""
    if not path:
        return False
    try:
        p = Path(str(path).replace("\\", "/")).expanduser().resolve()
    except OSError:
        return False
    if str(p) in ("/app", "/app/"):
        return True
    return is_router_package_root(p) and not _is_repo_root(p)


def infer_repo_root_from_absolute_path(raw: str) -> str:
    """Infer repo root from an absolute path string (no filesystem required)."""
    norm = str(raw or "").replace("\\", "/").strip().strip('",')
    if not norm.startswith("/"):
        return ""
    if "/router/" in norm:
        return norm.split("/router/", 1)[0]
    p = Path(norm)
    for parent in [p.parent, *p.parents]:
        if parent.name == "router" and len(parent.parent.parts) > 0:
            return str(parent.parent)
        if parent.name in ("docs", "scripts", "tmp") and len(parent.parent.parts) > 0:
            cand = parent.parent
            if (cand / "router" / "main.py").as_posix().replace("\\", "/") in norm or (
                str(cand) in norm
            ):
                return str(cand)
    return ""


def is_home_directory(path: Path) -> bool:
    try:
        p = path.expanduser().resolve()
    except OSError:
        return False
    home = Path.home().resolve()
    return p == home or (p.parent == Path("/home") and len(p.parts) == 3)


def _discover_repo_under_home(home: Path, *, max_depth: int = 4) -> list[Path]:
    """Scan under home for repo markers — no fixed subdirectory names."""
    found: list[Path] = []
    if not home.is_dir():
        return found
    base_depth = len(home.parts)
    try:
        for dirpath, dirnames, _filenames in os.walk(home, followlinks=False):
            depth = len(Path(dirpath).parts) - base_depth
            if depth > max_depth:
                dirnames.clear()
                continue
            cand = Path(dirpath)
            if _is_repo_root(cand):
                found.append(cand.resolve())
                dirnames.clear()
    except OSError:
        pass
    return found


def _candidates_from_hint(workspace_hint: str) -> list[Path]:
    out: list[Path] = []
    if not workspace_hint:
        return out
    try:
        wh = Path(workspace_hint).expanduser().resolve()
    except OSError:
        return out
    if not is_home_directory(wh):
        out.append(wh)
    for parent in [wh, *wh.parents]:
        if _is_repo_root(parent):
            out.append(parent)
    if is_home_directory(wh):
        out.extend(_discover_repo_under_home(wh))
    return out


def resolve_project_root(workspace_hint: str = "") -> str:
    """Priority: env PROJECT_ROOT → repo markers from hint → router parent → router dir."""
    env = os.getenv("PROJECT_ROOT", "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        if _is_repo_root(p):
            return str(p)

    seen: set[str] = set()
    for cand in _candidates_from_hint(workspace_hint):
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        if _is_repo_root(cand):
            return str(cand)

    parent = _ROUTER_DIR.parent
    if _is_repo_root(parent):
        return str(parent)
    if _is_repo_root(_ROUTER_DIR):
        return str(_ROUTER_DIR)

    return str(_ROUTER_DIR.parent if ( _ROUTER_DIR.parent / "docker-compose.yml").exists() else _ROUTER_DIR)


def effective_workspace(workspace_hint: str, known_files: list[str] | None = None) -> str:
    """Workspace for planner prompts — always repo root, not $HOME."""
    root = resolve_project_root(workspace_hint)
    if known_files:
        for p in known_files:
            if not p.startswith("/"):
                continue
            try:
                rp = Path(p).resolve()
                root_p = Path(root).resolve()
                if root_p in rp.parents or rp == root_p:
                    return str(root_p)
            except OSError:
                continue
    return root
