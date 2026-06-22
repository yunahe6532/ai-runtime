#!/usr/bin/env bash
# docs/*.md → HTML (Mermaid CDN 로드용 브라우저 미리보기)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCS_DIR="$ROOT/docs"
PORT="${PORT:-8098}"

cd "$ROOT/scripts/pdf-export"
[[ -d node_modules ]] || npm install

convert_md() {
  local input="$1"
  local output="${2:-${input%.md}.html}"
  node export-md-html.mjs "$input" "$output"
  echo "HTML: $output"
}

if [[ $# -ge 1 ]]; then
  INPUT="$1"
  [[ "$INPUT" != /* ]] && INPUT="$ROOT/$INPUT"
  OUTPUT="${2:-${INPUT%.md}.html}"
  convert_md "$INPUT" "$OUTPUT"
else
  shopt -s nullglob
  md_files=("$DOCS_DIR"/*.md)
  if [[ ${#md_files[@]} -eq 0 ]]; then
    echo "No .md files in $DOCS_DIR"
    exit 1
  fi
  echo "Converting ${#md_files[@]} markdown file(s) in $DOCS_DIR ..."
  for md in "${md_files[@]}"; do
    convert_md "$md"
  done
fi

echo ""
echo "브라우저에서 열기 (권장 — Mermaid CDN):"
echo "  ./scripts/serve-vision-html.sh"
echo "  → http://localhost:${PORT}/"
echo ""
echo "또는 file:// 직접 열기 (인터넷 필요)"
