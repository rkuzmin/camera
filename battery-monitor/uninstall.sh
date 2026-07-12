#!/usr/bin/env bash
# Remove the battery-monitor launchd service and binary.
# Config and state/logs are kept unless --purge is given.
set -euo pipefail

LABEL="local.battery-monitor"
BIN_PATH="$HOME/.local/bin/battery-monitor"
CONFIG_DIR="$HOME/.config/battery-monitor"
STATE_DIR="$HOME/.local/state/battery-monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

PURGE=0
case "${1:-}" in
    --purge) PURGE=1 ;;
    '') ;;
    *) echo "usage: $0 [--purge]" >&2; exit 2 ;;
esac

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST" "$BIN_PATH"
echo ">> service unloaded, removed $PLIST and $BIN_PATH"

if [ "$PURGE" = "1" ]; then
    rm -rf "$CONFIG_DIR" "$STATE_DIR"
    echo ">> purged $CONFIG_DIR and $STATE_DIR"
else
    echo ">> kept config ($CONFIG_DIR) and state/logs ($STATE_DIR); use --purge to remove"
fi
