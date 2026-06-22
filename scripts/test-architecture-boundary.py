#!/usr/bin/env python3
"""Pytest entry for architecture boundary rules."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_architecture_boundary() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-architecture-boundary.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_dependency_graph_verify() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate-dependency-graph.py"), "--verify"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "VERIFY OK" in proc.stdout
