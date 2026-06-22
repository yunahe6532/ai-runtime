#!/usr/bin/env bash
# Pre-deploy gate: run all router unit/smoke tests. Exit non-zero on any failure.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "=== verify-router.sh ==="
python3 scripts/benchmark-read-only-analysis-regression.py
python3 scripts/test-resolve-agent-phase-smoke.py
python3 scripts/test-evidence-judge.py
python3 scripts/test-planner-runtime-state-e2e.py
python3 scripts/test-explorer-trace-e2e.py
python3 scripts/test-llm-planner-shadow-e2e.py
python3 scripts/test-planner-promotion-gate-e2e.py
python3 scripts/test-project-index-ignore-e2e.py
python3 scripts/audit-runtime-reachability.py --static
python3 scripts/audit-runtime-reachability.py --profile
python3 scripts/audit-runtime-reachability.py --merge
python3 scripts/test-evidence-journal-report-e2e.py
python3 scripts/test-runtime-inspector.py
python3 scripts/test-agent-exec.py
python3 scripts/test-artifact-excerpt.py
echo "=== ALL VERIFY PASS ==="
