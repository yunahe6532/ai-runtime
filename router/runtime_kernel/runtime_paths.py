"""Runtime data path SSOT — generated artifacts live outside repo when possible."""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT: Path | None = None


def repo_root() -> Path:
    global _REPO_ROOT
    if _REPO_ROOT is None:
        _REPO_ROOT = Path(__file__).resolve().parents[2]
    return _REPO_ROOT


def runtime_data_dir() -> Path:
    env = (os.getenv("AI_RUNTIME_DATA_DIR") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".local" / "share" / "ai-runtime").resolve()


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_with_repo_fallback(primary: Path, repo_rel: str) -> Path:
    """Prefer external DATA_DIR; fall back to in-repo tmp only when it has content."""
    if primary.exists():
        try:
            if any(primary.iterdir()):
                return primary
        except OSError:
            pass
    fallback = repo_root() / repo_rel
    if fallback.exists():
        try:
            if any(fallback.iterdir()):
                return fallback
        except OSError:
            pass
    return _ensure_dir(primary)


def captures_dir() -> Path:
    env = (os.getenv("CAPTURE_HOST_DIR") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if Path("/captures").is_dir():
        return Path("/captures")
    return _resolve_with_repo_fallback(runtime_data_dir() / "captures", "tmp/cursor-captures")


def traces_dir() -> Path:
    env = (os.getenv("EXPLORER_TRACE_PATH") or "").strip()
    if env:
        return Path(env).expanduser().resolve().parent
    if Path("/captures").is_dir():
        return Path("/captures")
    return _ensure_dir(runtime_data_dir() / "traces")


def explorer_trace_file() -> Path:
    env = (os.getenv("EXPLORER_TRACE_PATH") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if Path("/captures").is_dir():
        return Path("/captures/explorer-trace.ndjson")
    primary = traces_dir() / "explorer-trace.ndjson"
    fallback = repo_root() / "tmp/cursor-captures/explorer-trace.ndjson"
    if primary.exists():
        return primary
    if fallback.exists():
        return fallback
    primary.parent.mkdir(parents=True, exist_ok=True)
    return primary


def context_cache_dir() -> Path:
    env = (os.getenv("CONTEXT_CACHE_HOST_DIR") or os.getenv("CONTEXT_CACHE_DIR") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if Path("/context-cache").is_dir():
        return Path("/context-cache")
    return _resolve_with_repo_fallback(
        runtime_data_dir() / "cache" / "context-cache",
        "tmp/context-cache",
    )


def benchmarks_dir() -> Path:
    env = (os.getenv("AI_RUNTIME_BENCHMARK_DIR") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return _ensure_dir(runtime_data_dir() / "benchmarks")


def reports_dir() -> Path:
    env = (os.getenv("AI_RUNTIME_REPORTS_DIR") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    repo_reports = repo_root() / "docs" / "reports"
    if repo_reports.is_dir():
        return repo_reports
    return _ensure_dir(runtime_data_dir() / "reports")


def journals_dir() -> Path:
    return _ensure_dir(runtime_data_dir() / "journals")


def artifacts_dir() -> Path:
    return _ensure_dir(runtime_data_dir() / "artifacts")


def indexes_dir() -> Path:
    return _ensure_dir(runtime_data_dir() / "indexes")


def repo_tmp_dir() -> Path:
    return repo_root() / "tmp"


def audit_json_dir() -> Path:
    """JSON audit outputs — prefer repo tmp for CI, else DATA_DIR reports."""
    d = repo_tmp_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d
