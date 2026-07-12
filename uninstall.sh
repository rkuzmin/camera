#!/usr/bin/env bash
# Remove the camera app launchd service and the battery watchdog.
# Keeps the repo, venv, recordings and config.json; --purge additionally
# removes the app service log and the watchdog's config/state (bot token).
set -euo pipefail

LABEL="local.camera-app"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/.local/state/camera-app"

PURGE=0
case "${1:-}" in
    --purge) PURGE=1 ;;
    '') ;;
    *) echo "usage: $0 [--purge]" >&2; exit 2 ;;
esac

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST"
echo ">> app service unloaded, removed $PLIST"
if [ "$PURGE" = 1 ]; then
    rm -rf "$LOG_DIR"
    echo ">> purged $LOG_DIR"
fi

if [ "$PURGE" = 1 ]; then
    "$SCRIPT_DIR/battery-monitor/uninstall.sh" --purge
else
    "$SCRIPT_DIR/battery-monitor/uninstall.sh"
fi
