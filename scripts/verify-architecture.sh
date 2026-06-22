#!/usr/bin/env bash
# CI-local architecture verification: boundary + dependency graph + memory bench.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

python3 scripts/check-architecture-boundary.py
python3 scripts/benchmark-memory-hierarchy.py --quality-gate
python3 scripts/benchmark-repeated-read-avoidance.py
python3 scripts/benchmark-memory-backend-swap.py
python3 scripts/generate-dependency-graph.py --verify
python3 scripts/test-architecture-boundary.py

echo "architecture verify: OK"
