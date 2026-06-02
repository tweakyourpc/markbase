#!/usr/bin/env bash
#
# MarkBase installer.
#
# Asks where the library should live (data is kept separate from this code
# folder), persists that choice, sets up the virtualenv + dependencies, and
# installs the systemd --user service. Re-runnable (idempotent).
#
# Non-interactive use:
#   ./install.sh --library /path/to/library [--state /path/to/state] [--yes]
#   MARKBASE_LIBRARY_PATH=/path ./install.sh --yes
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

CONFIG_DIR="$HOME/.config/markbase"
CONFIG_FILE="$CONFIG_DIR/markbase.env"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/markbase.service"

LIBRARY_ARG="${MARKBASE_LIBRARY_PATH:-}"
STATE_ARG="${MARKBASE_STATE_PATH:-}"
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --library) LIBRARY_ARG="$2"; shift 2 ;;
    --library=*) LIBRARY_ARG="${1#*=}"; shift ;;
    --state) STATE_ARG="$2"; shift 2 ;;
    --state=*) STATE_ARG="${1#*=}"; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Current configured path (if re-running).
CURRENT=""
if [ -f "$CONFIG_FILE" ]; then
  CURRENT="$(grep -E '^MARKBASE_LIBRARY_PATH=' "$CONFIG_FILE" | tail -1 | cut -d= -f2- || true)"
fi
DEFAULT_PATH="${LIBRARY_ARG:-${CURRENT:-$HOME/.local/share/markbase}}"

# ---- 1. Choose the library location -------------------------------------- #
if [ -n "$LIBRARY_ARG" ]; then
  LIB="$LIBRARY_ARG"
elif [ -t 0 ]; then
  echo "Where should the MarkBase library live? (Markdown + JSON; small files)"
  echo "It is kept separate from this code folder so backups and upgrades stay clean."
  read -r -p "Library path [$DEFAULT_PATH]: " LIB
  LIB="${LIB:-$DEFAULT_PATH}"
else
  LIB="$DEFAULT_PATH"
fi

# Expand ~ and make absolute.
LIB="${LIB/#\~/$HOME}"
mkdir -p "$LIB"
LIB="$(cd "$LIB" && pwd)"

STATE=""
if [ -n "$STATE_ARG" ]; then
  STATE="${STATE_ARG/#\~/$HOME}"
  mkdir -p "$STATE"; STATE="$(cd "$STATE" && pwd)"
fi

echo "==> Library: $LIB"
[ -n "$STATE" ] && echo "==> State (jobs.db): $STATE"

# ---- 2. Migrate an existing in-project library --------------------------- #
OLD="$APP_DIR/library"
if [ -d "$OLD" ] && [ "$OLD" != "$LIB" ] && [ -n "$(ls -A "$OLD" 2>/dev/null || true)" ]; then
  DO_MIGRATE=1
  if [ "$ASSUME_YES" -ne 1 ] && [ -t 0 ]; then
    read -r -p "Move existing library from $OLD to $LIB? [Y/n]: " ans
    case "${ans:-Y}" in [Nn]*) DO_MIGRATE=0 ;; esac
  fi
  if [ "$DO_MIGRATE" -eq 1 ]; then
    echo "==> Migrating existing library…"
    if command -v rsync >/dev/null 2>&1; then
      rsync -a "$OLD"/ "$LIB"/
    else
      cp -a "$OLD"/. "$LIB"/
    fi
    mv "$OLD" "$OLD.migrated-$(date +%Y%m%d%H%M%S)"
    echo "    done (original kept as $OLD.migrated-*)"
  fi
fi

# ---- 3. Virtualenv + dependencies ---------------------------------------- #
if [ ! -d "$APP_DIR/.venv" ]; then
  echo "==> Creating virtualenv"
  python3 -m venv "$APP_DIR/.venv"
fi
echo "==> Installing dependencies"
"$APP_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# ---- 4. Persist configuration -------------------------------------------- #
echo "==> Writing config to $CONFIG_FILE"
mkdir -p "$CONFIG_DIR"
{
  echo "# MarkBase configuration — edit and 'systemctl --user restart markbase' to apply."
  echo "MARKBASE_LIBRARY_PATH=$LIB"
  [ -n "$STATE" ] && echo "MARKBASE_STATE_PATH=$STATE"
} > "$CONFIG_FILE"

# ---- 5. Install the systemd --user unit ---------------------------------- #
echo "==> Installing systemd --user service"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=MarkBase — personal knowledge ingestion + reader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/run-service.sh
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

chmod +x "$APP_DIR/run-service.sh"
systemctl --user daemon-reload
systemctl --user enable markbase.service >/dev/null 2>&1 || true
systemctl --user restart markbase.service

# Keep the service alive across logout/reboot.
loginctl enable-linger "$USER" >/dev/null 2>&1 || true

sleep 3
PORT="$(portbroker get --name markbase 2>/dev/null || echo '?')"
echo
echo "✓ MarkBase installed."
echo "  Library: $LIB"
echo "  Service: systemctl --user status markbase   (logs: journalctl --user -u markbase -f)"
echo "  URL:     http://localhost:$PORT"
echo
echo "To change the library later: edit $CONFIG_FILE (or re-run ./install.sh) then"
echo "  systemctl --user restart markbase"
