#!/usr/bin/env bash
# Install battery-monitor as a service: a per-user LaunchAgent on macOS, or a
# systemd service + timer on Linux.
#
# Copies battery-monitor.sh to ~/.local/bin/battery-monitor, writes the config
# (bot token + chat id) to ~/.config/battery-monitor/config, registers the
# service and loads it. Re-running updates everything in place.
#
# On Linux the default is a system-wide unit (asks for sudo once) so checks
# run from boot, no login needed; --user-service installs user units instead
# (then enable lingering: sudo loginctl enable-linger <user>).
#
# Usage: ./install.sh [options]
#   --token <token>       Telegram bot token (or env TELEGRAM_BOT_TOKEN)
#   --chat-id <id>        Telegram chat id   (or env TELEGRAM_CHAT_ID)
#   --thresholds "20 10 5"  battery % levels for low-battery alerts
#   --interval <sec>      how often to check (default 60, min 10)
#   --lang <en|ru>        alert message language (default en)
#   --no-power-events     don't alert on AC <-> battery transitions
#   --proxy <url>         proxy for reaching api.telegram.org, e.g.
#                          socks5h://127.0.0.1:1080 — needed where Telegram
#                          is blocked on the network (e.g. hosting in Russia)
#   --user-service        Linux: systemd user units instead of system-wide
#   --no-test             don't send a test message after installing
#   --no-load             write files but don't register/start the service
#   -h, --help            show this help
#
# Without --token/--chat-id (and env vars) the script asks interactively.
# See README.md for how to create a bot and find your chat id.

set -euo pipefail

LABEL="local.battery-monitor"           # launchd label (macOS)
UNIT="battery-monitor"                  # systemd unit name (Linux)
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
BIN_DIR="$HOME/.local/bin"
BIN_PATH="$BIN_DIR/battery-monitor"
CONFIG_DIR="$HOME/.config/battery-monitor"
CONFIG_FILE="$CONFIG_DIR/config"
STATE_DIR="$HOME/.local/state/battery-monitor"
AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENTS_DIR/$LABEL.plist"
SYS_UNIT_DIR="/etc/systemd/system"
USER_UNIT_DIR="$HOME/.config/systemd/user"
OS="${OS_OVERRIDE:-$(uname -s)}"
USER_NAME=$(id -un)

TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${TELEGRAM_CHAT_ID:-}"
PROXY="${TELEGRAM_PROXY:-}"
THRESHOLDS="20 10 5"
INTERVAL=60
MSG_LANG="en"
POWER_EVENTS=1
RUN_TEST=1
LOAD=1
SCOPE="system"   # Linux only: system | user

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; }
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
        --proxy)           need_arg "$@"; PROXY="$2"; shift 2 ;;
        --user-service)    SCOPE="user"; shift ;;
        --no-test)         RUN_TEST=0; shift ;;
        --no-load)         LOAD=0; shift ;;
        -h|--help)         usage; exit 0 ;;
        *)                 usage >&2; die "unknown option: $1" ;;
    esac
done

# ----------------------------------------------------------------- checks ---

case "$OS" in
    Darwin)
        command -v pmset >/dev/null 2>&1 || die "pmset not found"
        if ! pmset -g batt | grep -qi 'InternalBattery'; then
            echo "warning: no battery detected on this Mac — low-battery and power-outage" >&2
            echo "warning: alerts will never fire; installing anyway." >&2
        fi
        ;;
    Linux)
        command -v curl >/dev/null 2>&1 || die "curl not found — install it (e.g. sudo apt install curl)"
        if ! grep -qs 'Battery' /sys/class/power_supply/*/type; then
            echo "warning: no battery found in /sys/class/power_supply — low-battery and" >&2
            echo "warning: power-outage alerts will never fire; installing anyway." >&2
        fi
        if [ "$LOAD" = 1 ]; then
            command -v systemctl >/dev/null 2>&1 || die "systemctl not found — this installer needs systemd"
        elif [ "$SCOPE" = "system" ]; then
            SCOPE="user"   # --no-load writes plain files only; system scope would need sudo
            echo ">> --no-load: writing user-scope units (no sudo)" >&2
        fi
        ;;
    *) die "unsupported OS: $OS (macOS and Linux only)" ;;
esac
[ -f "$SCRIPT_DIR/battery-monitor.sh" ] || die "battery-monitor.sh not found next to install.sh"

case "$MSG_LANG" in en|ru) ;; *) die "--lang must be en or ru" ;; esac
case "$INTERVAL" in ''|*[!0-9]*) die "--interval must be a number of seconds" ;; esac
[ "$INTERVAL" -ge 10 ] || die "--interval must be >= 10 seconds"
[ -n "$THRESHOLDS" ] || die "--thresholds cannot be empty"
for t in $THRESHOLDS; do
    case "$t" in ''|*[!0-9]*) die "--thresholds must be numbers, e.g. \"20 10 5\"" ;; esac
    [ "$t" -ge 1 ] && [ "$t" -le 100 ] || die "threshold $t is out of range 1..100"
done

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

mkdir -p "$BIN_DIR" "$CONFIG_DIR" "$STATE_DIR"

umask 077
{
    echo "# battery-monitor config — sourced by bash."
    echo "# Keep this file private: it contains the bot token (chmod 600)."
    printf 'TELEGRAM_BOT_TOKEN=%q\n' "$TOKEN"
    printf 'TELEGRAM_CHAT_ID=%q\n' "$CHAT_ID"
    echo "# Proxy for reaching api.telegram.org, e.g. socks5h://127.0.0.1:1080 —"
    echo "# set this if Telegram is blocked on this network (e.g. hosting in Russia)."
    printf 'TELEGRAM_PROXY=%q\n' "$PROXY"
    printf 'ALERT_THRESHOLDS=%q\n' "$THRESHOLDS"
    printf 'NOTIFY_POWER_EVENTS=%q\n' "$POWER_EVENTS"
    printf 'MESSAGE_LANG=%q\n' "$MSG_LANG"
    echo "# Optional: name shown in messages (default: this machine's hostname)."
    printf 'HOST_LABEL=%q\n' ""
    echo "# Set to 1 to log messages instead of sending them (testing)."
    echo "DRY_RUN=0"
} >"$CONFIG_FILE"
chmod 600 "$CONFIG_FILE"

install -m 755 "$SCRIPT_DIR/battery-monitor.sh" "$BIN_PATH"

# ---------------------------------------------------------------- service ---

if [ "$OS" = "Darwin" ]; then
    mkdir -p "$AGENTS_DIR"
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

    if [ "$LOAD" = 1 ]; then
        launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST"
        echo ">> service $LABEL loaded (checks every ${INTERVAL}s)"
    else
        echo ">> --no-load: files written, service not loaded"
    fi
else
    emit_service_unit() {  # $1 = system|user
        cat <<EOF
[Unit]
Description=Battery/power Telegram watchdog (single check)

[Service]
Type=oneshot
EOF
        if [ "$1" = "system" ]; then
            printf 'User=%s\n' "$USER_NAME"
            printf 'Environment=HOME=%s\n' "$HOME"
        fi
        printf 'ExecStart="%s" check\n' "$BIN_PATH"
    }
    emit_timer_unit() {
        cat <<EOF
[Unit]
Description=Run battery-monitor every ${INTERVAL} seconds

[Timer]
OnBootSec=45s
OnUnitActiveSec=${INTERVAL}s
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF
    }

    if [ "$SCOPE" = "system" ] && [ "$LOAD" = 1 ]; then
        if ! sudo -v; then
            echo "warning: no sudo — falling back to user units; for headless operation run:" >&2
            echo "warning:   sudo loginctl enable-linger $USER_NAME" >&2
            SCOPE="user"
        fi
    fi

    if [ "$SCOPE" = "system" ]; then
        emit_service_unit system | sudo tee "$SYS_UNIT_DIR/$UNIT.service" >/dev/null
        emit_timer_unit          | sudo tee "$SYS_UNIT_DIR/$UNIT.timer"   >/dev/null
        sudo systemctl daemon-reload
        sudo systemctl enable --now "$UNIT.timer"
        sudo systemctl start "$UNIT.service" || true   # first check right away
        echo ">> system units $UNIT.service + $UNIT.timer enabled (every ${INTERVAL}s, from boot)"
    else
        mkdir -p "$USER_UNIT_DIR"
        emit_service_unit user > "$USER_UNIT_DIR/$UNIT.service"
        emit_timer_unit        > "$USER_UNIT_DIR/$UNIT.timer"
        if [ "$LOAD" = 1 ]; then
            systemctl --user daemon-reload
            systemctl --user enable --now "$UNIT.timer"
            systemctl --user start "$UNIT.service" || true
            echo ">> user units $UNIT.service + $UNIT.timer enabled (every ${INTERVAL}s)"
            if command -v loginctl >/dev/null 2>&1; then
                if [ "$(loginctl show-user "$USER_NAME" --property=Linger --value 2>/dev/null || true)" != "yes" ]; then
                    echo "warning: user services stop when you log out; for 24/7 operation run:" >&2
                    echo "warning:   sudo loginctl enable-linger $USER_NAME" >&2
                fi
            fi
        else
            echo ">> --no-load: user units written to $USER_UNIT_DIR, not enabled"
        fi
    fi
fi

# ------------------------------------------------------------------- test ---

if [ "$RUN_TEST" = 1 ]; then
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

if [ "$OS" = "Darwin" ]; then
    SERVICE_HINT="launchctl print gui/\$(id -u)/$LABEL"
elif [ "$SCOPE" = "system" ]; then
    SERVICE_HINT="systemctl status $UNIT.timer"
else
    SERVICE_HINT="systemctl --user status $UNIT.timer"
fi
cat <<EOF
>> installed:
   binary   $BIN_PATH
   config   $CONFIG_FILE  (edit + no reload needed)
   log      $STATE_DIR/monitor.log

   status:    $BIN_PATH status
   service:   $SERVICE_HINT
   watch log: tail -f $STATE_DIR/monitor.log
   uninstall: $SCRIPT_DIR/uninstall.sh [--purge]
EOF
