#!/usr/bin/env bash
# codeflow lint — static checks for flow files (see engine/lint.py)
#   bash scripts/lint.sh                # lint the configured workflows folder
#   bash scripts/lint.sh --strict       # fail on warnings too
#   bash scripts/lint.sh path/to/flow.py
set -e
cd "$(dirname "$0")/.."
[ -f .codeflow.env ] && . ./.codeflow.env
PY="./venv/bin/python"
[ -x "$PY" ] || PY="python3"
TARGET="${CODEFLOW_WORKFLOWS_DIR:-workflows}"
# a path argument overrides the configured folder
for a in "$@"; do case "$a" in -*) ;; *) TARGET="$a";; esac; done
exec "$PY" -m engine.lint "$TARGET" "$@"
