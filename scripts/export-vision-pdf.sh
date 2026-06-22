#!/usr/bin/env bash
# VISION.md (또는 지정 md) → PDF (Mermaid 포함)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INPUT="${1:-$ROOT/docs/VISION.md}"
OUTPUT="${2:-${INPUT%.md}.pdf}"
cd "$ROOT/scripts/pdf-export"
if [[ ! -d node_modules ]]; then
  npm install
fi
node export-md-pdf.mjs "$INPUT" "$OUTPUT"
