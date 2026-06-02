#!/usr/bin/env bash
#
# MarkBase launcher.
# Creates a virtual environment (if missing), installs dependencies, and
# starts the FastAPI server.
#
set -euo pipefail

cd "$(dirname "$0")"

# Load persisted config (library location) if the installer wrote one.
CONFIG_FILE="${MARKBASE_CONFIG:-$HOME/.config/markbase/markbase.env}"
if [ -f "$CONFIG_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
fi

VENV_DIR="${VENV_DIR:-.venv}"
HOST="${HOST:-0.0.0.0}"
if [ -z "${PORT:-}" ]; then
  PORT="$(portbroker get --name markbase-dev 2>/dev/null || portbroker alloc --name markbase-dev --host "$HOST" --persistent)"
fi
export MARKBASE_HOST="$HOST"
export MARKBASE_PORT="$PORT"

if [ ! -d "$VENV_DIR" ]; then
  echo "==> Creating virtual environment in $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "==> Installing dependencies"
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

echo "==> Library path: ${MARKBASE_LIBRARY_PATH:-./library}"
echo "==> Starting MarkBase on http://$HOST:$PORT"
exec uvicorn app:app --host "$HOST" --port "$PORT"
