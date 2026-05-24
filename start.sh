#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${AUTO_MARKETPLACE_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

cd "$APP_DIR"

if [ -f "$APP_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$APP_DIR/.env"
    set +a
fi

DATA_DIR="${AUTO_MARKETPLACE_DATA_DIR:-$APP_DIR/data}"
NOVNC_DIR="${AUTO_MARKETPLACE_NOVNC_DIR:-$APP_DIR/noVNC}"

export AUTO_MARKETPLACE_APP_DIR="$APP_DIR"
export AUTO_MARKETPLACE_DATA_DIR="$DATA_DIR"
export AUTO_MARKETPLACE_NOVNC_DIR="$NOVNC_DIR"

mkdir -p "$DATA_DIR"

if [ -f "$APP_DIR/venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$APP_DIR/venv/bin/activate"
fi

if [ ! -d "$NOVNC_DIR" ]; then
    echo "noVNC not found at $NOVNC_DIR; downloading it now..."
    wget -qO- https://github.com/novnc/noVNC/archive/v1.4.0.tar.gz | tar xz
    mv noVNC-1.4.0 "$NOVNC_DIR"
    wget -qO- https://github.com/novnc/websockify/archive/v0.11.0.tar.gz | tar xz
    mv websockify-0.11.0 "$NOVNC_DIR/utils/websockify"
fi

echo "Starting Auto-Lister on 0.0.0.0:8000"
exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
