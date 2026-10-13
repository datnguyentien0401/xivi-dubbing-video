#!/usr/bin/env bash
# Runs before Gemini CLI ends a session (the "AfterAgent" hook) — a quick check that the project
# is still in a buildable/testable state, to avoid leaving broken code behind.
set -uo pipefail

# The hook may be invoked with a cwd other than the project root — always cd to the root first,
# relying on $GEMINI_PROJECT_DIR if set, otherwise inferring it from this file's location
# (.gemini/hooks/verify.sh -> go up 2 levels).
ROOT="${GEMINI_PROJECT_DIR:-}"
if [ -z "$ROOT" ]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
cd "$ROOT" || { echo "verify.sh: project root not found ($ROOT)"; exit 1; }

echo "== verify.sh: quick check of the project's state (root: $ROOT) =="

STATUS=0

if [ -f backend/pyproject.toml ] || [ -f backend/requirements.txt ]; then
  RUFF_BIN="ruff"
  if ! command -v ruff >/dev/null 2>&1; then
    if [ -x backend/.venv/bin/ruff ]; then
      RUFF_BIN="$ROOT/backend/.venv/bin/ruff"
    else
      RUFF_BIN=""
    fi
  fi
  if [ -n "$RUFF_BIN" ]; then
    echo "-- ruff check backend --"
    (cd backend && "$RUFF_BIN" check .) || STATUS=1
  else
    echo "-- ruff check backend: skipped (ruff not found, including in backend/.venv) --"
  fi
fi

if [ -f frontend/package.json ]; then
  if [ -d frontend/node_modules ]; then
    echo "-- tsc --noEmit frontend --"
    (cd frontend && npx --no-install tsc --noEmit) || STATUS=1
  else
    echo "-- tsc --noEmit frontend: skipped (node_modules not found) --"
  fi
fi

if [ "$STATUS" -ne 0 ]; then
  echo "verify.sh: ERRORS FOUND — review the output above before considering the task complete."
else
  echo "verify.sh: OK"
fi
exit 0
