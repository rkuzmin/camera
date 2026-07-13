#!/usr/bin/env bash
# Remove the battery-monitor service (launchd on macOS, systemd on Linux —
# whichever scope is found) and the installed binary.
# Config and state/logs are kept unless --purge is given.
set -euo pipefail

LABEL="local.battery-monitor"
UNIT="battery-monitor"
BIN_PATH="$HOME/.local/bin/battery-monitor"
CONFIG_DIR="$HOME/.config/battery-monitor"
STATE_DIR="$HOME/.local/state/battery-monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
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
    echo ">> service unloaded, removed $PLIST"
else
    if [ -f "$USER_UNIT_DIR/$UNIT.timer" ] || [ -f "$USER_UNIT_DIR/$UNIT.service" ]; then
        systemctl --user disable --now "$UNIT.timer" >/dev/null 2>&1 || true
        rm -f "$USER_UNIT_DIR/$UNIT.timer" "$USER_UNIT_DIR/$UNIT.service"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        echo ">> user units removed"
    fi
    if [ -f "$SYS_UNIT_DIR/$UNIT.timer" ] || [ -f "$SYS_UNIT_DIR/$UNIT.service" ]; then
        echo ">> removing system units (needs sudo)..."
        sudo systemctl disable --now "$UNIT.timer" >/dev/null 2>&1 || true
        sudo rm -f "$SYS_UNIT_DIR/$UNIT.timer" "$SYS_UNIT_DIR/$UNIT.service"
        sudo systemctl daemon-reload
        echo ">> system units removed"
    fi
fi
rm -f "$BIN_PATH"
echo ">> removed $BIN_PATH"

if [ "$PURGE" = 1 ]; then
    rm -rf "$CONFIG_DIR" "$STATE_DIR"
    echo ">> purged $CONFIG_DIR and $STATE_DIR"
else
    echo ">> kept config ($CONFIG_DIR) and state/logs ($STATE_DIR); use --purge to remove"
fi
