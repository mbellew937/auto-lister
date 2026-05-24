#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${AUTO_MARKETPLACE_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
LOG_FILE="${AUTO_MARKETPLACE_INSTALL_LOG:-$APP_DIR/install.log}"
exec > "$LOG_FILE" 2>&1

while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do sleep 1; done
while fuser /var/lib/apt/lists/lock >/dev/null 2>&1; do sleep 1; done

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates \
    fonts-dejavu \
    fonts-liberation \
    openbox \
    python3-pip \
    python3-venv \
    wget \
    x11-utils \
    x11vnc \
    xvfb

cd "$APP_DIR"
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
playwright install-deps chromium
playwright install chromium

if [ ! -d "$APP_DIR/noVNC" ]; then
    wget -qO- https://github.com/novnc/noVNC/archive/v1.4.0.tar.gz | tar xz
    mv noVNC-1.4.0 "$APP_DIR/noVNC"
    wget -qO- https://github.com/novnc/websockify/archive/v0.11.0.tar.gz | tar xz
    mv websockify-0.11.0 "$APP_DIR/noVNC/utils/websockify"
fi

mkdir -p "${AUTO_MARKETPLACE_DATA_DIR:-$APP_DIR/data}"
echo "Install complete. Copy .env.example to .env, fill in your keys, then run ./start.sh."
