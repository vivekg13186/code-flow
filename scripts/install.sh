#!/usr/bin/env bash
# code flow installer — macOS & Linux
# Creates a virtualenv, installs dependencies, lets you choose where your
# workflows live, and writes the config to .codeflow.env
set -e
cd "$(dirname "$0")/.."
ROOT="$PWD"

echo "== code flow install =="

# --- python check -----------------------------------------------------------
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "ERROR: Python 3.10+ not found. Install it from https://www.python.org/downloads/"
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "ERROR: Python 3.10+ required, found: $($PY --version)"
  exit 1
fi
echo "using $($PY --version) at $(command -v $PY)"

# --- venv + deps -------------------------------------------------------------
if [ ! -d venv ]; then
  "$PY" -m venv venv
fi
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q
echo "dependencies installed"

# --- workflows path ----------------------------------------------------------
printf "Workflows folder [%s]: " "$ROOT/workflows"
read -r WF
WF="${WF:-$ROOT/workflows}"
# expand ~ and make absolute
WF="${WF/#\~/$HOME}"
mkdir -p "$WF"
WF="$(cd "$WF" && pwd)"

# seed an empty custom folder with the sample flows
if [ "$WF" != "$ROOT/workflows" ] && [ -z "$(ls -A "$WF" 2>/dev/null)" ]; then
  printf "Folder is empty — copy the sample flows into it? [Y/n]: "
  read -r SEED
  if [ "${SEED:-Y}" != "n" ] && [ "${SEED:-Y}" != "N" ]; then
    cp -R "$ROOT/workflows/." "$WF/"
    find "$WF" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    echo "sample flows copied"
  fi
fi

# --- config ------------------------------------------------------------------
PORT="${CODEFLOW_PORT:-8000}"
cat > .codeflow.env <<EOF
CODEFLOW_WORKFLOWS_DIR=$WF
CODEFLOW_ENVIRONMENTS_DIR=$ROOT/environments
CODEFLOW_HISTORY_DIR=$ROOT/history
CODEFLOW_HOST=127.0.0.1
CODEFLOW_PORT=$PORT
EOF
echo "config written to .codeflow.env:"
sed 's/^/  /' .codeflow.env

echo ""
echo "Done. Start the server with:  bash scripts/start.sh"
