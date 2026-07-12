#!/usr/bin/env bash
# Install battery-monitor as a per-user launchd service (macOS).
#
# Copies battery-monitor.sh to ~/.local/bin/battery-monitor, writes the config
# (bot token + chat id) to ~/.config/battery-monitor/config, generates a
# LaunchAgent plist and loads it. Re-running updates everything in place.
#
# Usage: ./install.sh [options]
#   --token <token>       Telegram bot token (or env TELEGRAM_BOT_TOKEN)
#   --chat-id <id>        Telegram chat id   (or env TELEGRAM_CHAT_ID)
#   --thresholds "20 10 5"  battery % levels for low-battery alerts
#   --interval <sec>      how often to check (default 60, min 10)
#   --lang <en|ru>        alert message language (default en)
#   --no-power-events     don't alert on AC <-> battery transitions
#   --no-test             don't send a test message after installing
#   --no-load             write files but don't (re)load the launchd service
#   -h, --help            show this help
#
# Without --token/--chat-id (and env vars) the script asks interactively.
# See README.md for how to create a bot and find your chat id.

set -euo pipefail

LABEL="local.battery-monitor"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
BIN_DIR="$HOME/.local/bin"
BIN_PATH="$BIN_DIR/battery-monitor"
CONFIG_DIR="$HOME/.config/battery-monitor"
CONFIG_FILE="$CONFIG_DIR/config"
STATE_DIR="$HOME/.local/state/battery-monitor"
AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENTS_DIR/$LABEL.plist"

TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${TELEGRAM_CHAT_ID:-}"
THRESHOLDS="20 10 5"
INTERVAL=60
MSG_LANG="en"
POWER_EVENTS=1
RUN_TEST=1
LOAD=1

usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "install.sh: $*" >&2; exit 1; }

need_arg() { [ $# -ge 2 ] || die "option $1 needs a value"; }
while [ $# -gt 0 ]; do
    case "$1" in
        --token)           need_arg "$@"; TOKEN="$2"; shift 2 ;;
        --chat-id)         need_arg "$@"; CHAT_ID="$2"; shift 2 ;;
        --thresholds)      need_arg "$@"; THRESHOLDS="$2"; shift 2 ;;
        --interval)        need_arg "$@"; INTERVAL="$2"; shift 2 ;;
        --lang)            need_arg "$@"; MSG_LANG="$2"; shift 2 ;;
        --no-power-events) POWER_EVENTS=0; shift ;;
        --no-test)         RUN_TEST=0; shift ;;
        --no-load)         LOAD=0; shift ;;
        -h|--help)         usage; exit 0 ;;
        *)                 usage >&2; die "unknown option: $1" ;;
    esac
done

# ----------------------------------------------------------------- checks ---

[ "$(uname -s)" = "Darwin" ] || die "this installer is macOS-only (launchd + pmset)"
command -v pmset >/dev/null 2>&1 || die "pmset not found"
[ -f "$SCRIPT_DIR/battery-monitor.sh" ] || die "battery-monitor.sh not found next to install.sh"

case "$MSG_LANG" in en|ru) ;; *) die "--lang must be en or ru" ;; esac
case "$INTERVAL" in ''|*[!0-9]*) die "--interval must be a number of seconds" ;; esac
[ "$INTERVAL" -ge 10 ] || die "--interval must be >= 10 seconds"
[ -n "$THRESHOLDS" ] || die "--thresholds cannot be empty"
for t in $THRESHOLDS; do
    case "$t" in ''|*[!0-9]*) die "--thresholds must be numbers, e.g. \"20 10 5\"" ;; esac
    [ "$t" -ge 1 ] && [ "$t" -le 100 ] || die "threshold $t is out of range 1..100"
done

if ! pmset -g batt | grep -qi 'InternalBattery'; then
    echo "warning: no battery detected on this Mac — low-battery and power-outage" >&2
    echo "warning: alerts will never fire; installing anyway." >&2
fi

# ------------------------------------------------------------ credentials ---

if [ -z "$TOKEN" ] && [ -t 0 ]; then
    printf 'Telegram bot token (from @BotFather): '
    read -r TOKEN
fi
if [ -z "$CHAT_ID" ] && [ -t 0 ]; then
    printf 'Telegram chat id (send the bot a message, then see README.md): '
    read -r CHAT_ID
fi
[ -n "$TOKEN" ] || die "bot token missing — pass --token, set TELEGRAM_BOT_TOKEN, or run interactively"
[ -n "$CHAT_ID" ] || die "chat id missing — pass --chat-id, set TELEGRAM_CHAT_ID, or run interactively"

case "$TOKEN" in
    *:*) ;;
    *) echo "warning: token doesn't look like '<digits>:<secret>' from @BotFather" >&2 ;;
esac
case "$CHAT_ID" in
    -*|*[0-9]*) ;;
    *) echo "warning: chat id is usually a number like 123456789 or -100..." >&2 ;;
esac

# ------------------------------------------------------------------ files ---

mkdir -p "$BIN_DIR" "$CONFIG_DIR" "$STATE_DIR" "$AGENTS_DIR"

umask 077
{
    echo "# battery-monitor config — sourced by bash."
    echo "# Keep this file private: it contains the bot token (chmod 600)."
    printf 'TELEGRAM_BOT_TOKEN=%q\n' "$TOKEN"
    printf 'TELEGRAM_CHAT_ID=%q\n' "$CHAT_ID"
    printf 'ALERT_THRESHOLDS=%q\n' "$THRESHOLDS"
    printf 'NOTIFY_POWER_EVENTS=%q\n' "$POWER_EVENTS"
    printf 'MESSAGE_LANG=%q\n' "$MSG_LANG"
    echo "# Optional: name shown in messages (default: this Mac's computer name)."
    printf 'HOST_LABEL=%q\n' ""
    echo "# Set to 1 to log messages instead of sending them (testing)."
    echo "DRY_RUN=0"
} >"$CONFIG_FILE"
chmod 600 "$CONFIG_FILE"

install -m 755 "$SCRIPT_DIR/battery-monitor.sh" "$BIN_PATH"

cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$BIN_PATH</string>
        <string>check</string>
    </array>
    <key>StartInterval</key>
    <integer>$INTERVAL</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>$STATE_DIR/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>$STATE_DIR/launchd.log</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null

# ---------------------------------------------------------------- service ---

if [ "$LOAD" = "1" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo ">> service $LABEL loaded (checks every ${INTERVAL}s)"
else
    echo ">> --no-load: files written, service not loaded"
fi

if [ "$RUN_TEST" = "1" ]; then
    echo ">> sending a test message to the bot..."
    if "$BIN_PATH" test; then
        echo ">> test message sent — check your Telegram"
    else
        echo ">> test message FAILED — check the token/chat id in $CONFIG_FILE" >&2
        echo ">>   (make sure you pressed Start in the bot's chat), then run:" >&2
        echo ">>   $BIN_PATH test" >&2
        exit 1
    fi
fi

cat <<EOF
>> installed:
   binary   $BIN_PATH
   config   $CONFIG_FILE  (edit + no reload needed)
   plist    $PLIST
   log      $STATE_DIR/monitor.log

   status:    $BIN_PATH status
   watch log: tail -f $STATE_DIR/monitor.log
   uninstall: $SCRIPT_DIR/uninstall.sh [--purge]
EOF
