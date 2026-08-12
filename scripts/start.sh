#!/usr/bin/env bash
# code flow — start the server (macOS & Linux)
# Reads .codeflow.env (created by scripts/install.sh) and runs the app.
set -e
cd "$(dirname "$0")/.."

if [ ! -d venv ]; then
  echo "No venv found — run:  bash scripts/install.sh"
  exit 1
fi

if [ -f .codeflow.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.codeflow.env
  set +a
fi

URL="http://${CODEFLOW_HOST:-127.0.0.1}:${CODEFLOW_PORT:-8000}"
echo "code flow → $URL"
echo "workflows: ${CODEFLOW_WORKFLOWS_DIR:-$PWD/workflows}"
echo "(Ctrl+C to stop)"

# open the browser once the server is up
if command -v open >/dev/null 2>&1; then       # macOS
  (sleep 1.5 && open "$URL") &
elif command -v xdg-open >/dev/null 2>&1; then # Linux desktop
  (sleep 1.5 && xdg-open "$URL" >/dev/null 2>&1) &
fi

exec ./venv/bin/python app.py
