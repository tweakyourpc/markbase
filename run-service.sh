#!/usr/bin/env bash
#
# Service launcher for MarkBase (used by the systemd --user unit).
# Reads the persisted library location from the config file written by
# install.sh, resolves the port from portbroker, then execs uvicorn from the
# project virtualenv so yt-dlp / markitdown are on PATH.
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

# Load persisted configuration (MARKBASE_LIBRARY_PATH, optional MARKBASE_STATE_PATH).
CONFIG_FILE="${MARKBASE_CONFIG:-$HOME/.config/markbase/markbase.env}"
if [ -f "$CONFIG_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
fi

export PATH="$APP_DIR/.venv/bin:$HOME/bin:$HOME/.local/bin:$PATH"
# Fall back to an in-project library only if nothing was configured.
export MARKBASE_LIBRARY_PATH="${MARKBASE_LIBRARY_PATH:-$APP_DIR/library}"

# Persistent named reservation; allocate once, reuse forever.
PORT="$(portbroker get --name markbase 2>/dev/null || portbroker alloc --name markbase --host 0.0.0.0 --persistent)"
export MARKBASE_HOST="0.0.0.0"
export MARKBASE_PORT="$PORT"

echo "MarkBase starting on 0.0.0.0:${PORT} (library: ${MARKBASE_LIBRARY_PATH}, state: ${MARKBASE_STATE_PATH:-<library>})"
exec "$APP_DIR/.venv/bin/uvicorn" app:app --host 0.0.0.0 --port "$PORT"
