"""Resolved source registry — LLM picks source_id; runtime resolves paths."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_ROUTER_DIR = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("router.source_registry")

READ_ONLY_MIN_DIR_GREP_FILES = int(os.getenv("READ_ONLY_MIN_DIR_GREP_FILES", "2"))
WIDE_DIR_GREP_PATTERN = "."

TOOL_RESULT_ERROR_MARKERS = (
    "error: file not found",
    "file not found",
    "path does not exist",
    "no such file",
    "cannot find",
    "failed to read",
    "exit code",
)

SOURCE_TOOL_NAMES = frozenset({"ReadSource", "GrepSource", "GlobSource"})
LEGACY_READ_TOOLS = frozenset({"Read", "Grep", "Glob"})


@dataclass
class RootMapping:
    host: str
    container: str
    confidence: float = 0.0
    method: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceEntry:
    id: str
    label: str
    relpath: str
    host_path: str
    container_path: str
    kind: str  # file | dir
    exists: bool
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceRegistry:
    root: RootMapping
    sources: list[SourceEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root.to_dict(),
            "sources": [s.to_dict() for s in self.sources],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SourceRegistry:
        if not data:
            return cls(root=RootMapping(host="", container=""))
        root = RootMapping(**{k: data.get("root", {}).get(k, "") for k in RootMapping.__dataclass_fields__})
        if isinstance(data.get("root"), dict):
            root = RootMapping(
                host=str(data["root"].get("host") or ""),
                container=str(data["root"].get("container") or ""),
                confidence=float(data["root"].get("confidence") or 0),
                method=str(data["root"].get("method") or ""),
            )
        sources: list[SourceEntry] = []
        for raw in data.get("sources") or []:
            if not isinstance(raw, dict):
                continue
            fields = {k: raw[k] for k in SourceEntry.__dataclass_fields__ if k in raw}
            sources.append(SourceEntry(**fields))
        return cls(root=root, sources=sources)

    def get(self, source_id: str) -> SourceEntry | None:
        for s in self.sources:
            if s.id == source_id:
                return s
        return None

    def available(self) -> list[SourceEntry]:
        return [s for s in self.sources if s.exists]

    def source_ids(self) -> list[str]:
        return [s.id for s in self.available()]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def source_id_for_relpath(relpath: str) -> str:
    rel = str(relpath).replace("\\", "/").strip("/")
    if not rel:
        return "src.root"
    if rel.endswith(".md"):
        stem = _slug(Path(rel).stem)
        return f"doc.{stem or 'doc'}"
    parts = rel.split("/")
    if parts[0] in DOC_DIR_NAMES:
        return f"doc.{_slug(Path(parts[-1]).stem)}"
    if len(parts) == 1:
        return f"dir.{_slug(parts[0])}"
    if len(parts) == 2:
        return f"dir.{_slug(parts[1])}"
    return f"src.{_slug(rel.replace('/', '_'))}"


def lookup_source_id_by_relpath(
    registry: SourceRegistry | dict[str, Any],
    relpath: str,
) -> str | None:
    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    norm = str(relpath).replace("\\", "/").strip().lower().lstrip("./").rstrip("/")
    if not norm:
        return None
    for entry in reg.sources:
        if not entry.exists:
            continue
        rel = entry.relpath.replace("\\", "/").strip().lower().rstrip("/")
        if rel == norm or rel.endswith("/" + norm) or norm.endswith("/" + rel):
            return entry.id
        if norm.endswith(".md") and Path(rel).name.lower() == Path(norm).name.lower():
            return entry.id
    return None


def resolve_path_via_registry(
    registry: SourceRegistry | dict[str, Any],
    path_hint: str,
    *,
    for_cursor: bool = True,
) -> tuple[str, str]:
    """Map LLM path hint (relative or wrong absolute) → resolved path + source_id."""
    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    hint = str(path_hint or "").replace("\\", "/").strip()
    if not hint:
        raise ValueError("empty path hint")

    sid = lookup_source_id_by_relpath(reg, hint)
    if sid:
        return resolve_source_path(reg, sid, for_cursor=for_cursor), sid

    name = Path(hint.lstrip("/")).name.lower()
    for entry in reg.available():
        if entry.relpath.lower().endswith(name) or Path(entry.relpath).name.lower() == name:
            return (
                entry.host_path if for_cursor else entry.container_path,
                entry.id,
            )

    if not hint.startswith("/"):
        joined = Path(reg.root.host) / hint.lstrip("/")
        if joined.exists():
            sid2 = lookup_source_id_by_relpath(reg, hint) or source_id_for_relpath(hint)
            return str(joined.resolve()), sid2 or ""

    raise KeyError(f"cannot resolve path via registry: {path_hint!r}")


def _is_router_package_root(path: Path) -> bool:
    from .project_root import is_router_package_root

    return is_router_package_root(path)


def _relpath_candidates(rel: str, container_root: Path, host_root: Path) -> list[str]:
    """Map repo-relative hints to paths valid under container or host layout."""
    norm = str(rel).replace("\\", "/").strip("/")
    if not norm:
        return []
    cands = [norm]
    router_pkg = _is_router_package_root(container_root)
    repo_host = not _is_router_package_root(host_root) and (host_root / "router").is_dir()

    if norm.startswith("router/"):
        cands.append(norm[len("router/") :])
    elif router_pkg and not norm.startswith("docs/"):
        cands.append(f"router/{norm}")

    if norm.startswith("docs/") and router_pkg and repo_host:
        cands.append(norm)

    out: list[str] = []
    for c in cands:
        if c and c not in out:
            out.append(c)
    return out


def _resolve_entry_paths(
    mapping: RootMapping,
    rel: str,
) -> tuple[str, Path, Path, bool, str]:
    """Pick best host/container paths and existence for one registry entry."""
    from .project_root import is_container_router_path

    container_root = Path(mapping.container)
    host_root = Path(mapping.host)
    router_pkg = _is_router_package_root(container_root)
    repo_host = (
        not _is_router_package_root(host_root)
        and str(host_root.resolve()) != str(container_root.resolve())
        and not is_container_router_path(str(host_root))
    )

    best_rel = rel.strip("/")
    best_host = host_root / best_rel
    best_container = container_root / best_rel
    best_exists = False
    best_kind = "file"

    for cand in _relpath_candidates(rel, container_root, host_root):
        cp = container_root / cand
        hp = host_root / cand

        if repo_host and cand.startswith("docs/"):
            hp = host_root / cand
            cp = container_root / cand
        elif repo_host and router_pkg and not cand.startswith("docs/"):
            host_rel = cand if cand.startswith("router/") else f"router/{cand}"
            hp = host_root / host_rel
            cp = container_root / cand

        exists = cp.exists() or hp.exists()
        if not exists:
            continue

        kind = "dir"
        if cp.exists():
            kind = "dir" if cp.is_dir() else "file"
        elif hp.exists():
            kind = "dir" if hp.is_dir() else "file"
        elif not cand.endswith(".md") and not Path(cand).suffix:
            kind = "dir"

        best_rel = cand
        best_host = hp
        best_container = cp
        best_exists = True
        best_kind = kind
        break

    if not best_exists:
        for cand in _relpath_candidates(rel, container_root, host_root):
            cp = container_root / cand
            hp = host_root / cand
            if repo_host and router_pkg and not cand.startswith("docs/"):
                host_rel = cand if cand.startswith("router/") else f"router/{cand}"
                hp = host_root / host_rel
            best_rel = cand
            best_host = hp
            best_container = cp
            if repo_host and cand.startswith("docs/"):
                best_exists = True
                best_kind = "file"
            break
        if not best_rel.endswith(".md") and not Path(best_rel).suffix:
            best_kind = "dir"

    return best_rel, best_host, best_container, best_exists, best_kind


def _infer_host_root_from_paths(paths: list[str]) -> str:
    from .project_root import _is_repo_root

    for raw in paths:
        if not raw or raw.startswith("cluster:"):
            continue
        try:
            p = Path(str(raw).replace("\\", "/"))
            if not p.is_absolute() and str(raw).startswith("./"):
                p = Path(str(raw)[2:])
            for parent in [p, *p.parents]:
                if _is_repo_root(parent):
                    return str(parent.resolve())
        except OSError:
            continue
    return ""


def _host_root_from_context_cache() -> str:
    """Infer host repo root from mounted context-cache project registry."""
    from .project_root import _is_repo_root, effective_workspace, is_container_router_path

    cache_dir = os.getenv("CONTEXT_CACHE_DIR", "").strip() or "/context-cache"
    reg_path = Path(cache_dir) / "projects" / "_registry.json"
    if not reg_path.is_file():
        return ""
    try:
        data = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return ""
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return ""
    for meta in projects.values():
        if not isinstance(meta, dict):
            continue
        ws = str(meta.get("workspace") or "").strip()
        if not ws or is_container_router_path(ws):
            continue
        try:
            wh = Path(ws).expanduser()
        except OSError:
            continue
        if wh.is_dir():
            root = effective_workspace(ws)
            if root and not is_container_router_path(root) and _is_repo_root(Path(root)):
                return root
    return _host_root_from_context_cache_artifacts(Path(cache_dir))


def _host_root_from_context_cache_artifacts(cache_dir: Path) -> str:
    """Scan cached request/index payloads for absolute repo roots."""
    from .project_root import _is_repo_root, infer_repo_root_from_absolute_path

    if not cache_dir.is_dir():
        return ""
    seen: set[str] = set()
    patterns = (
        cache_dir / "raw",
        cache_dir / "index",
        cache_dir / "projects",
    )
    path_re = re.compile(r"(/[\w./~-]+)")
    for base in patterns:
        if not base.is_dir():
            continue
        for fp in base.rglob("*.json"):
            try:
                text = fp.read_text(encoding="utf-8", errors="ignore")[:120000]
            except OSError:
                continue
            for m in path_re.finditer(text):
                raw = m.group(1).rstrip('",')
                root = infer_repo_root_from_absolute_path(raw)
                if not root or root in seen:
                    continue
                seen.add(root)
                rp = Path(root)
                if rp.is_dir() and _is_repo_root(rp):
                    return root
                if "/router/" in raw or "/docs/" in raw:
                    return root
    return ""


def resolve_root_mapping(
    workspace_hint: str = "",
    *,
    known_paths: list[str] | None = None,
) -> RootMapping:
    """Resolve host/container project roots from metadata — no fixed user paths."""
    router_dir = Path(__file__).resolve().parents[1]
    from .project_root import (
        _discover_repo_under_home,
        _is_repo_root,
        is_container_router_path,
        resolve_project_root,
    )

    container_default = os.getenv("CONTAINER_PROJECT_ROOT", "").strip()
    if not container_default:
        parent = router_dir.parent
        container_default = str(parent if _is_repo_root(parent) else router_dir)

    mount_map = os.getenv("ROOT_MOUNT_MAP", "").strip()
    if mount_map and ":" in mount_map:
        cont, host = mount_map.split(":", 1)
        hp = Path(host).expanduser()
        if _is_repo_root(hp):
            return RootMapping(
                host=str(hp.resolve()),
                container=str(Path(cont).resolve()),
                confidence=0.99,
                method="env_mount_map",
            )

    host_root = _infer_host_root_from_paths(list(known_paths or []))
    method = "known_path"
    confidence = 0.97 if host_root else 0.0

    ws = workspace_hint
    if is_container_router_path(ws):
        ws = ""

    if not host_root:
        host_root = resolve_project_root(ws or workspace_hint)
        method = "repo_marker"
        confidence = 0.94 if _is_repo_root(Path(host_root)) else 0.5

    if (
        not host_root
        or is_container_router_path(host_root)
        or host_root == container_default
        or not _is_repo_root(Path(host_root))
    ):
        env_root = os.getenv("PROJECT_ROOT", "").strip()
        if env_root:
            ep = Path(env_root).expanduser()
            if _is_repo_root(ep):
                host_root = str(ep.resolve())
                method = "env_project_root"
                confidence = 0.98

    if not host_root or is_container_router_path(host_root) or not _is_repo_root(Path(host_root)):
        cached = _host_root_from_context_cache()
        if cached:
            host_root = cached
            method = "context_cache_registry"
            confidence = 0.92

    if not host_root or is_container_router_path(host_root) or not _is_repo_root(Path(host_root)):
        for repo in _discover_repo_under_home(Path.home(), max_depth=5):
            host_root = str(repo)
            method = "home_scan"
            confidence = 0.85
            break

    if is_container_router_path(workspace_hint) or str(container_default) == str(router_dir):
        method = "container_mount" if method == "repo_marker" else method
        confidence = max(confidence, 0.9)

    return RootMapping(
        host=host_root,
        container=container_default,
        confidence=confidence,
        method=method,
    )


def build_source_registry(
    mapping: RootMapping,
    relpaths: list[str],
) -> SourceRegistry:
    """Turn relative hints into resolved, existence-checked source entries."""
    seen_ids: set[str] = set()
    entries: list[SourceEntry] = []

    for raw in relpaths:
        rel_hint = str(raw).replace("\\", "/").strip("/")
        if not rel_hint:
            continue
        sid = source_id_for_relpath(rel_hint)
        base = sid
        n = 2
        while sid in seen_ids:
            sid = f"{base}_{n}"
            n += 1
        seen_ids.add(sid)

        rel, host_path, container_path, exists, kind = _resolve_entry_paths(mapping, rel_hint)
        label = rel if rel.endswith("/") else rel + ("/" if kind == "dir" else "")

        entries.append(
            SourceEntry(
                id=sid,
                label=label,
                relpath=rel,
                host_path=str(host_path),
                container_path=str(container_path),
                kind=kind,
                exists=exists,
                confidence=mapping.confidence if exists else 0.0,
            )
        )

    return SourceRegistry(root=mapping, sources=entries)


# --- Read-only discovery (no hardcoded module/doc paths) ---

DISCOVERY_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".codex",
        "__pycache__",
        "node_modules",
        ".venv",
        ".venv-llamaindex",
        "venv",
        "site-packages",
        "dist",
        "build",
        ".npm",
        "tmp",
        "cache",
        "vendor",
        "target",
        "coverage",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
    }
)

COMMON_CODE_ROOT_NAMES = (
    "src",
    "lib",
    "pkg",
    "app",
    "apps",
    "packages",
    "internal",
    "cmd",
    "server",
    "servers",
    "services",
    "api",
    "components",
)

DOC_DIR_NAMES = ("docs", "doc", "documentation")

ROOT_DOC_NAMES = ("README.md", "Readme.md", "readme.md", "CONTRIBUTING.md", "CHANGELOG.md")

# Generic dev-doc filename stems (not project-specific)
DOC_NAME_HINT_RE = re.compile(
    r"(readme|architecture|arch|structure|design|overview|contributing|changelog|"
    r"module|modules|guide|spec|api|docs|documentation|getting.?started)",
    re.I,
)

QUERY_TOKEN_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "read",
        "only",
        "code",
        "edit",
        "file",
        "files",
        "project",
        "structure",
        "summary",
        "role",
        "roles",
        "analyze",
        "analysis",
        "please",
        "help",
        "what",
        "where",
        "how",
        "역할",
        "요약",
        "분석",
        "구조",
        "프로젝트",
        "코드",
        "수정",
        "말고",
        "읽어서",
        "근거",
        "함께",
        "답해",
        "알려",
        "설명",
        "아키텍처",
    }
)


def _discovery_scan_bases(host: Path) -> list[Path]:
    """Common dev layout roots under project host (generic, not repo-specific)."""
    bases: list[Path] = [host]
    for name in COMMON_CODE_ROOT_NAMES:
        p = host / name
        if p.is_dir() and p not in bases:
            bases.append(p)
    return bases


def _relpath_is_excluded(relpath: str) -> bool:
    parts = Path(str(relpath).replace("\\", "/")).parts
    return any(
        p in DISCOVERY_EXCLUDE_DIRS or p.startswith(".venv") or p == "site-packages"
        for p in parts
    )


def _query_identifiers(query: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", query or ""):
        t = raw.strip("_")
        if len(t) < 3 or t.lower() in QUERY_TOKEN_STOP:
            continue
        if t not in tokens:
            tokens.append(t)
    return tokens[:12]


def _relpath_from_host(host: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(host.resolve()).as_posix()
    except ValueError:
        return path.name


def _find_relpaths_for_token(host: Path, token: str) -> list[str]:
    """Match query token to existing dirs/files under generic dev layout roots."""
    token_l = token.lower()
    found: list[str] = []
    for base in _discovery_scan_bases(host):
        direct = base / token
        if direct.exists():
            found.append(_relpath_from_host(host, direct))
        direct_md = base / f"{token}.md"
        if direct_md.is_file():
            found.append(_relpath_from_host(host, direct_md))
        if base.is_dir():
            try:
                for child in base.iterdir():
                    if child.name.lower() == token_l and child.is_dir():
                        found.append(_relpath_from_host(host, child))
            except OSError:
                pass
    if len(found) < 2:
        try:
            for path in host.rglob(token):
                if _relpath_is_excluded(_relpath_from_host(host, path)):
                    continue
                if path.name.lower() != token_l:
                    continue
                if path.is_dir() or path.suffix.lower() == ".md":
                    rel = _relpath_from_host(host, path)
                    if rel not in found:
                        found.append(rel)
                if len(found) >= 3:
                    break
        except OSError:
            pass
    return list(dict.fromkeys(found))[:3]


def _score_doc_relpath(relpath: str, query: str) -> float:
    name = Path(relpath).stem.lower()
    q = (query or "").lower()
    score = 1.0
    if name in ("readme", "read_me"):
        score += 4.0
    if DOC_NAME_HINT_RE.search(name):
        score += 5.0
    if name in q or name.replace("_", " ") in q:
        score += 2.0
    return score


def _discovery_scan_root(mapping: RootMapping) -> Path | None:
    """Pick a filesystem root that exists in *this* process (host mount or container)."""
    host = Path(str(mapping.host or "")).expanduser()
    container = Path(str(mapping.container or "")).expanduser()
    if host.is_dir():
        return host.resolve()
    if container.is_dir():
        return container.resolve()
    router_dir = Path(__file__).resolve().parents[1]
    if router_dir.is_dir():
        return router_dir.resolve()
    return None


def resolve_source_id(
    registry: SourceRegistry | dict[str, Any],
    hint: str,
) -> str | None:
    """Resolve LLM source_id hint (dir.foo, foo, relpath) to registry id."""
    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    raw = str(hint or "").strip()
    if not raw:
        return None
    entry = reg.get(raw)
    if entry and entry.exists:
        return entry.id
    bare = raw
    if bare.startswith("dir."):
        bare = bare[4:]
    elif bare.startswith("doc."):
        bare = bare[4:]
    candidate = f"dir.{_slug(bare)}"
    entry = reg.get(candidate)
    if entry and entry.exists:
        return entry.id
    doc_candidate = f"doc.{_slug(bare)}"
    entry = reg.get(doc_candidate)
    if entry and entry.exists:
        return entry.id
    by_rel = lookup_source_id_by_relpath(reg, raw)
    if by_rel:
        return by_rel
    name_l = bare.lower()
    for s in reg.available():
        if Path(s.relpath).name.lower() == name_l:
            return s.id
    return None


def discover_read_only_relpaths(
    query: str,
    mapping: RootMapping,
    *,
    max_total: int = 12,
    max_summary_docs: int = 3,
) -> tuple[list[str], list[str]]:
    """Discover registry relpaths from filesystem + query (no preset module list).

    Returns (all_relpaths, summary_doc_relpaths).
    """
    scan_root = _discovery_scan_root(mapping)
    if scan_root is None:
        return [], []

    scored: list[tuple[float, str]] = []

    for doc_name in ROOT_DOC_NAMES:
        p = scan_root / doc_name
        if p.is_file():
            scored.append((_score_doc_relpath(doc_name, query), doc_name))

    for docs_dir_name in DOC_DIR_NAMES:
        docs_dir = scan_root / docs_dir_name
        if not docs_dir.is_dir():
            continue
        for md in sorted(docs_dir.glob("*.md")):
            rel = f"{docs_dir_name}/{md.name}"
            scored.append((_score_doc_relpath(rel, query), rel))

    for token in _query_identifiers(query):
        for rel in _find_relpaths_for_token(scan_root, token):
            bonus = 8.0 if (scan_root / rel).is_dir() else 6.0
            scored.append((bonus, rel))

    scored.sort(key=lambda x: (-x[0], x[1]))
    all_relpaths: list[str] = []
    for _, rel in scored:
        if _relpath_is_excluded(rel):
            continue
        if rel not in all_relpaths:
            all_relpaths.append(rel)
        if len(all_relpaths) >= max_total:
            break

    summary_docs: list[str] = []
    for score, rel in sorted(scored, key=lambda x: (-x[0], x[1])):
        if _relpath_is_excluded(rel):
            continue
        if not rel.endswith(".md"):
            continue
        if score < 4.0 and len(summary_docs) >= 1:
            continue
        if rel not in summary_docs:
            summary_docs.append(rel)
        if len(summary_docs) >= max_summary_docs:
            break

    return all_relpaths, summary_docs


def summary_source_ids_for_registry(
    registry: SourceRegistry | dict[str, Any],
    summary_relpaths: list[str],
) -> list[str]:
    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    rel_set = {str(r).replace("\\", "/").strip("/").lower() for r in summary_relpaths}
    out: list[str] = []
    for entry in reg.sources:
        if not entry.exists or entry.kind != "file":
            continue
        rel = entry.relpath.replace("\\", "/").strip("/").lower()
        if rel in rel_set and entry.id not in out:
            out.append(entry.id)
    return out


def required_source_ids_from_plan(
    plan_dict: dict[str, Any],
    registry: SourceRegistry | dict[str, Any],
) -> list[str]:
    explicit = list(plan_dict.get("required_source_ids") or [])
    if explicit:
        return explicit
    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    candidates = set(plan_dict.get("source_candidates") or [])
    summary = list(plan_dict.get("summary_source_ids") or [])
    if summary:
        return summary
    return [
        s.id
        for s in reg.sources
        if s.exists and (not candidates or s.id in candidates)
    ]


def resolve_source_path(registry: SourceRegistry | dict[str, Any], source_id: str, *, for_cursor: bool = True) -> str:
    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    entry = reg.get(source_id)
    if not entry:
        raise KeyError(f"unknown source_id: {source_id}")
    if not entry.exists:
        raise FileNotFoundError(f"source not available: {source_id}")
    return entry.host_path if for_cursor else entry.container_path


def is_tool_result_success(content: str, *, tool_name: str = "") -> bool:
    text = (content or "").strip()
    if len(text) < 8:
        return False
    low = text.lower()
    if any(m in low for m in TOOL_RESULT_ERROR_MARKERS):
        return False
    if low.startswith("error:") or low.startswith("error "):
        return False
    tool = str(tool_name or "").strip()
    if tool in ("Glob", "GlobSource"):
        if (
            "0 files" in low
            or "no files found" in low
            or "found 0" in low
            or "0 matches" in low
        ):
            return False
    if tool in ("Grep", "GrepSource"):
        if "no matches" in low or "0 matches" in low or "found 0" in low:
            return False
    return True


def relpath_for_source_id(registry: SourceRegistry | dict[str, Any], source_id: str) -> str:
    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    entry = reg.get(source_id)
    return entry.relpath if entry else ""


def register_source_hit(
    plan_dict: dict[str, Any],
    source_id: str,
    *,
    success: bool,
    content: str,
    registry: SourceRegistry | dict[str, Any],
    tool_name: str = "",
    pattern: str = "",
) -> list[str]:
    """Record coverage by source_id — only on successful, substantive reads."""
    if not success or not is_tool_result_success(content, tool_name=tool_name):
        return []
    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    entry = reg.get(source_id)
    if not entry or not entry.exists:
        return []

    tool = str(tool_name or "").strip()
    if not tool:
        low_head = (content or "")[:800].lower()
        if "<workspace_result" in low_head:
            tool = "Grep"
        elif "result of search" in low_head:
            tool = "Glob"
        elif entry.kind == "file":
            tool = "Read"

    if tool in ("Grep", "GrepSource") and entry.kind == "dir":
        fc = grep_workspace_file_count(content)
        record_source_grep_depth(plan_dict, source_id, fc)
        min_needed = min_files_for_dir_source(entry)
        if fc < min_needed:
            record_source_inventory_fail(plan_dict, source_id, "Grep")
            LOG.info(
                "source_hit shallow grep sid=%s files=%d min=%d escalate=GlobSource",
                source_id,
                fc,
                min_needed,
            )
            return []

    if tool in ("Glob", "GlobSource") and entry.kind == "dir":
        fc = glob_workspace_file_count(content)
        record_source_grep_depth(plan_dict, source_id, fc)
        min_needed = min(READ_ONLY_MIN_DIR_GREP_FILES, min_files_for_dir_source(entry))
        if fc < min_needed:
            record_source_inventory_fail(plan_dict, source_id, "Glob")
            LOG.info(
                "source_hit shallow glob sid=%s files=%d min=%d",
                source_id,
                fc,
                min_needed,
            )
            return []
        try:
            from .read_only_explorer import record_source_exploration_stage

            record_source_exploration_stage(plan_dict, source_id, "inventory")
        except ImportError:
            pass

    if tool in ("Read", "ReadSource") and entry.kind == "file":
        try:
            from .read_only_explorer import record_source_exploration_stage

            record_source_exploration_stage(plan_dict, source_id, "anchor")
        except ImportError:
            pass

    hits = list(plan_dict.get("coverage_hits") or [])
    sid_hits = list(plan_dict.get("source_hits") or [])
    added: list[str] = []

    if source_id not in sid_hits:
        sid_hits.append(source_id)
        plan_dict["source_hits"] = sid_hits
        added.append(source_id)

    rel = entry.relpath
    norm_rel = rel.replace("\\", "/").lower().rstrip("/")
    if norm_rel not in {str(h).lower().rstrip("/") for h in hits}:
        hits.append(rel)
        plan_dict["coverage_hits"] = hits
        added.append(rel)

    try:
        from .read_only_explorer import note_exploration_from_hit

        note_exploration_from_hit(
            plan_dict,
            source_id,
            tool=tool,
            content=content,
            pattern=pattern,
            registry=reg,
        )
    except ImportError:
        pass

    return added


def source_coverage_passes(plan_dict: dict[str, Any], registry: SourceRegistry | dict[str, Any]) -> bool:
    from .target_coverage import target_coverage_passes

    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    if read_only_docs_sufficient(plan_dict, reg):
        return True

    pending = pending_source_ids_for_plan(plan_dict, reg)
    if pending:
        return False

    source_hits = set(plan_dict.get("source_hits") or [])
    required_ids = required_source_ids_from_plan(plan_dict, reg)
    if required_ids:
        missing = [sid for sid in required_ids if sid not in source_hits]
        if missing:
            return False
        return True

    targets = list(plan_dict.get("preferred_sources") or [])
    ctx = plan_dict.get("context_need") if isinstance(plan_dict.get("context_need"), dict) else {}
    if ctx.get("coverage_targets"):
        targets = list(dict.fromkeys(targets + list(ctx.get("coverage_targets") or [])))
    return target_coverage_passes(plan_dict, targets)


def read_only_docs_sufficient(
    plan_dict: dict[str, Any],
    registry: SourceRegistry | dict[str, Any],
) -> bool:
    """Enough summary docs read for read_only structure answer (registry-derived, not fixed paths)."""
    if str(plan_dict.get("router_intent") or "") != "read_only_analysis":
        return False
    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    hits = set(plan_dict.get("source_hits") or [])

    summary_ids = list(plan_dict.get("summary_source_ids") or [])
    if summary_ids:
        return all(sid in hits for sid in summary_ids)

    doc_ids = [
        s.id
        for s in reg.sources
        if s.exists and s.kind == "file" and s.id.startswith("doc.")
    ]
    if not doc_ids:
        return False
    hit_docs = [sid for sid in doc_ids if sid in hits]
    readme_ids = [
        s.id
        for s in reg.sources
        if s.exists
        and s.kind == "file"
        and Path(s.relpath).name.lower().startswith("readme")
    ]
    if readme_ids and any(rid in hits for rid in readme_ids):
        return True
    return len(hit_docs) >= 1


def llm_source_tool_definitions(source_ids: list[str]) -> list[dict[str, Any]]:
    if not source_ids:
        return []
    enum_ids = list(dict.fromkeys(source_ids))
    return [
        {
            "type": "function",
            "function": {
                "name": "ReadSource",
                "description": "Read a project file by source_id. Do not invent paths.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string", "enum": enum_ids},
                    },
                    "required": ["source_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "GrepSource",
                "description": "Search within a registered source by source_id. For dir sources runtime uses wide grep (pattern .) to list all files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string", "enum": enum_ids},
                        "pattern": {"type": "string"},
                    },
                    "required": ["source_id", "pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "GlobSource",
                "description": "Glob under a registered directory source_id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string", "enum": enum_ids},
                        "glob_pattern": {"type": "string"},
                    },
                    "required": ["source_id", "glob_pattern"],
                },
            },
        },
    ]


def expand_source_tool_call(
    tool_name: str,
    args: dict[str, Any],
    registry: SourceRegistry | dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Convert LLM source_id tool call to Cursor Read/Grep/Glob."""
    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    sid = resolve_source_id(reg, str(args.get("source_id") or ""))
    if not sid:
        return None
    try:
        path = resolve_source_path(reg, sid, for_cursor=True)
    except (KeyError, FileNotFoundError):
        return None

    entry = reg.get(sid)
    if tool_name == "ReadSource":
        if entry and entry.kind == "dir":
            return "Glob", {
                "glob_pattern": str(args.get("glob_pattern") or "*"),
                "target_directory": path,
                "_source_id": sid,
            }
        return "Read", {"path": path, "_source_id": sid}
    if tool_name == "GrepSource":
        grep_path = path
        pattern = wide_grep_pattern(entry, str(args.get("pattern") or ""))
        return "Grep", {
            "pattern": pattern,
            "path": grep_path,
            "_source_id": sid,
        }
    if tool_name == "GlobSource":
        pattern = str(args.get("glob_pattern") or "*").strip() or "*"
        if entry and entry.kind == "dir" and pattern in ("*.md", "**/*.md"):
            return "Grep", {
                "pattern": ".",
                "path": path,
                "_source_id": sid,
            }
        target = path if entry and entry.kind == "dir" else str(Path(path).parent)
        return "Glob", {
            "glob_pattern": pattern if pattern != "**/*" else "*",
            "target_directory": target,
            "_source_id": sid,
        }
    return None


def grep_workspace_file_count(content: str) -> int:
    try:
        from artifact_excerpt import count_grep_files

        return count_grep_files(content)
    except ImportError:
        return 0


_GLOB_PATH_RE = re.compile(
    r"([\w./~-]+\.(?:py|md|yml|yaml|json|ts|tsx|js|sh|toml))\s*$",
    re.I,
)


def glob_workspace_file_count(content: str) -> int:
    """Count distinct file paths in a Glob tool result."""
    if not (content or "").strip():
        return 0
    seen: set[str] = set()
    for ln in content.splitlines():
        s = ln.strip()
        if not s or s.startswith("<") or s.lower().startswith("result of search"):
            continue
        if s.lower().startswith("total ") or "files):" in s.lower():
            continue
        m = _GLOB_PATH_RE.search(s.replace("\\", "/"))
        if m:
            seen.add(m.group(1).lower())
    return len(seen)


def record_source_inventory_fail(plan_dict: dict[str, Any], source_id: str, tool: str) -> None:
    fails = dict(plan_dict.get("source_inventory_failures") or {})
    key = f"{source_id}:{tool}"
    fails[key] = int(fails.get(key, 0) or 0) + 1
    plan_dict["source_inventory_failures"] = fails


def dir_inventory_tool_for_source(plan_dict: dict[str, Any], source_id: str) -> str:
    """Glob lists all files; Cursor Grep on dirs only enumerates one file."""
    _ = plan_dict, source_id
    return "GlobSource"


def wide_grep_pattern(entry: SourceEntry | None, pattern: str) -> str:
    """Directory grep: honor explicit LLM/explorer patterns; default `.` when empty."""
    p = (pattern or "").strip()
    if entry and entry.kind == "dir":
        if p and p != ".":
            return p
        return WIDE_DIR_GREP_PATTERN
    return p or WIDE_DIR_GREP_PATTERN


def min_files_for_dir_source(entry: SourceEntry | None) -> int:
    if not entry or entry.kind != "dir":
        return 1
    cp = Path(str(entry.container_path or "")).expanduser()
    if cp.is_dir():
        try:
            py_count = sum(1 for _ in cp.glob("*.py"))
            if py_count > 0:
                return min(py_count, max(READ_ONLY_MIN_DIR_GREP_FILES, py_count // 2 + 1))
        except OSError:
            pass
    return READ_ONLY_MIN_DIR_GREP_FILES


def record_source_grep_depth(plan_dict: dict[str, Any], source_id: str, file_count: int) -> None:
    depths = dict(plan_dict.get("source_grep_depth") or {})
    depths[source_id] = max(int(depths.get(source_id, 0) or 0), int(file_count))
    plan_dict["source_grep_depth"] = depths


def pending_source_ids_for_plan(
    plan_dict: dict[str, Any],
    registry: SourceRegistry | dict[str, Any],
) -> list[str]:
    """source_ids still needing collection — missing hit or shallow dir exploration."""
    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    required = required_source_ids_from_plan(plan_dict, reg)
    hits = set(plan_dict.get("source_hits") or [])
    pending: list[str] = []
    try:
        from .read_only_explorer import dir_exploration_sufficient
    except ImportError:
        dir_exploration_sufficient = None  # type: ignore[assignment,misc]

    for sid in required:
        entry = reg.get(sid)
        if not entry:
            pending.append(sid)
            continue
        if entry.kind == "dir":
            if dir_exploration_sufficient and dir_exploration_sufficient(plan_dict, sid):
                continue
            pending.append(sid)
            continue
        if sid not in hits:
            pending.append(sid)
    return pending


def missing_source_ids_for_plan(
    plan_dict: dict[str, Any],
    registry: SourceRegistry | dict[str, Any],
) -> list[str]:
    return pending_source_ids_for_plan(plan_dict, registry)


def format_source_registry_block(registry: SourceRegistry | dict[str, Any]) -> str:
    reg = registry if isinstance(registry, SourceRegistry) else SourceRegistry.from_dict(registry)
    lines = [
        "[Available Sources]",
        f"project_root_host: {reg.root.host}",
        f"project_root_container: {reg.root.container}",
        "Pick source_id only — do NOT invent absolute paths.",
    ]
    for s in reg.available():
        lines.append(f"- {s.id}: {s.label} ({s.kind})")
    if not reg.available():
        lines.append("- (no verified sources — wait for registry refresh)")
    lines.append("[/Available Sources]")
    return "\n".join(lines)
