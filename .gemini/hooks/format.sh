#!/usr/bin/env bash
# Auto-formats a file Gemini CLI just edited. Receives the file path via argument,
# environment variable, or parsed from hook stdin JSON.
set -euo pipefail

ROOT="${GEMINI_PROJECT_DIR:-}"
if [ -z "$ROOT" ]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
cd "$ROOT" || exit 0

FILE="${1:-${GEMINI_FILE_PATH:-}}"

if [ -z "$FILE" ] && [ ! -t 0 ]; then
  # Read stdin JSON payload if piped
  STDIN_INPUT="$(cat || true)"
  if [ -n "$STDIN_INPUT" ]; then
    FILE="$(python3 -c "import sys, json
try:
    d = json.loads('''$STDIN_INPUT''')
    print(d.get('tool_input', {}).get('file_path', ''))
except Exception:
    pass" 2>/dev/null || true)"
  fi
fi

[ -z "$FILE" ] && exit 0
[ ! -f "$FILE" ] && exit 0

case "$FILE" in
  *.py)
    command -v ruff >/dev/null 2>&1 && ruff format "$FILE" || true
    ;;
  *.ts|*.tsx|*.js|*.jsx)
    if [ -f frontend/package.json ]; then
      (cd frontend && npx --no-install prettier --write "${FILE#frontend/}") 2>/dev/null || true
    fi
    ;;
  *)
    ;;
esac
exit 0
