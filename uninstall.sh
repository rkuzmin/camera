#!/usr/bin/env bash
# Remove the camera app service (launchd on macOS, systemd on Linux) and the
# battery watchdog. Keeps the repo, venv, recordings and config.json; --purge
# additionally removes the app service log (macOS) and the watchdog's
# config/state (bot token).
set -euo pipefail

LABEL="local.camera-app"
UNIT="camera-app"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/.local/state/camera-app"
SYS_UNIT_DIR="/etc/systemd/system"
USER_UNIT_DIR="$HOME/.config/systemd/user"
OS="${OS_OVERRIDE:-$(uname -s)}"

PURGE=0
case "${1:-}" in
    --purge) PURGE=1 ;;
    '') ;;
    *) echo "usage: $0 [--purge]" >&2; exit 2 ;;
esac

if [ "$OS" = "Darwin" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    rm -f "$PLIST"
    echo ">> app service unloaded, removed $PLIST"
else
    if [ -f "$USER_UNIT_DIR/$UNIT.service" ]; then
        systemctl --user disable --now "$UNIT.service" >/dev/null 2>&1 || true
        rm -f "$USER_UNIT_DIR/$UNIT.service"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        echo ">> app user service removed"
    fi
    if [ -f "$SYS_UNIT_DIR/$UNIT.service" ]; then
        echo ">> removing app system service (needs sudo)..."
        sudo systemctl disable --now "$UNIT.service" >/dev/null 2>&1 || true
        sudo rm -f "$SYS_UNIT_DIR/$UNIT.service"
        sudo systemctl daemon-reload
        echo ">> app system service removed"
    fi
fi
if [ "$PURGE" = 1 ] && [ -d "$LOG_DIR" ]; then
    rm -rf "$LOG_DIR"
    echo ">> purged $LOG_DIR"
fi

if [ "$PURGE" = 1 ]; then
    "$SCRIPT_DIR/battery-monitor/uninstall.sh" --purge
else
    "$SCRIPT_DIR/battery-monitor/uninstall.sh"
fi
