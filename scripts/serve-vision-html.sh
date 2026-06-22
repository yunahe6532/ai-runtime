#!/usr/bin/env bash
# docs/ 폴더 HTTP 서빙 — Mermaid·CSS CDN과 함께 HTML 미리보기
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8098}"

"$ROOT/scripts/export-vision-html.sh" >/dev/null 2>&1 || "$ROOT/scripts/export-vision-html.sh"

echo "Serving http://localhost:${PORT}/"
echo "  예: VISION.html, ARCHITECTURE.html, WORKSPACE_LAYOUT.html"
echo "Ctrl+C 종료"
cd "$ROOT/docs"
exec python3 -m http.server "$PORT"
