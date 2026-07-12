#!/usr/bin/env bash
# Install the camera app as a per-user launchd service (macOS), together with
# the battery/power Telegram watchdog from battery-monitor/.
#
#   local.camera-app       — app.py under the repo venv; starts at login,
#                            restarts if it crashes (KeepAlive)
#   local.battery-monitor  — pings a Telegram bot on power outage / low battery
#
# If the venv is missing it is created (.venv) and requirements installed.
#
# Usage: ./install.sh [options]
#   --dir <path>          app directory to run from (default: this repo)
#   --app-only            install only the camera app service
#   --battery-only        install only the battery watchdog
#   --no-load             write files but don't (re)load the services
#   battery watchdog options are passed through: --token, --chat-id,
#   --thresholds, --interval, --lang en|ru, --no-power-events, --no-test
#   (without --token/--chat-id the watchdog installer asks interactively;
#    see battery-monitor/README.md for creating the bot)
#
# Stop/start later:  launchctl bootout gui/$(id -u)/local.camera-app
#                    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.camera-app.plist
# Restart:           launchctl kickstart -k gui/$(id -u)/local.camera-app

set -euo pipefail

LABEL="local.camera-app"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENTS_DIR/$LABEL.plist"
LOG_DIR="$HOME/.local/state/camera-app"

APP_DIR="$SCRIPT_DIR"
INSTALL_APP=1
INSTALL_BATTERY=1
LOAD=1
BATTERY_ARGS=()

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "install.sh: $*" >&2; exit 1; }
need_arg() { [ $# -ge 2 ] || die "option $1 needs a value"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dir)             need_arg "$@"; APP_DIR=$(cd "$2" && pwd) || die "bad --dir"; shift 2 ;;
        --app-only)        INSTALL_BATTERY=0; shift ;;
        --battery-only)    INSTALL_APP=0; shift ;;
        --no-load)         LOAD=0; BATTERY_ARGS+=(--no-load); shift ;;
        --token|--chat-id|--thresholds|--interval|--lang)
                           need_arg "$@"; BATTERY_ARGS+=("$1" "$2"); shift 2 ;;
        --no-power-events|--no-test)
                           BATTERY_ARGS+=("$1"); shift ;;
        -h|--help)         usage; exit 0 ;;
        *)                 usage >&2; die "unknown option: $1" ;;
    esac
done
[ "$INSTALL_APP" = 1 ] || [ "$INSTALL_BATTERY" = 1 ] || die "--app-only and --battery-only exclude each other"

[ "$(uname -s)" = "Darwin" ] || die "this installer is macOS-only (launchd)"

# ------------------------------------------------------- camera app service ---

if [ "$INSTALL_APP" = 1 ]; then
    [ -f "$APP_DIR/app.py" ] || die "app.py not found in $APP_DIR (use --dir <path-to-repo>)"
    case "$APP_DIR" in
        */.claude/worktrees/*)
            echo "warning: $APP_DIR looks like a temporary worktree — the service will" >&2
            echo "warning: break when it's removed. Run from your main checkout or use --dir." >&2 ;;
    esac

    # Find (or create) the venv and make sure dependencies are importable.
    PY=""
    for v in .venv venv; do
        if [ -x "$APP_DIR/$v/bin/python" ]; then PY="$APP_DIR/$v/bin/python"; break; fi
    done
    if [ -z "$PY" ]; then
        command -v python3 >/dev/null 2>&1 || die "python3 not found — install the Xcode command line tools"
        echo ">> no venv found — creating $APP_DIR/.venv and installing requirements"
        echo ">>   (first install downloads OpenCV/ultralytics — this can take a while)"
        python3 -m venv "$APP_DIR/.venv"
        PY="$APP_DIR/.venv/bin/python"
        "$PY" -m pip install --quiet --upgrade pip
        "$PY" -m pip install -r "$APP_DIR/requirements.txt"
    fi
    if ! "$PY" -c 'import flask' >/dev/null 2>&1; then
        echo ">> venv at $(dirname "$(dirname "$PY")") is missing dependencies — installing"
        "$PY" -m pip install -r "$APP_DIR/requirements.txt"
        "$PY" -c 'import flask' >/dev/null 2>&1 \
            || die "dependencies still missing after pip install — check the pip output above"
    fi

    mkdir -p "$AGENTS_DIR" "$LOG_DIR"
    cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$APP_DIR/app.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$APP_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/app.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/app.log</string>
</dict>
</plist>
EOF
    plutil -lint "$PLIST" >/dev/null

    if [ "$LOAD" = 1 ]; then
        if lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
            echo "warning: something already listens on port 8080 (the app started by hand?)." >&2
            echo "warning: stop it, or the service will crash-loop until the port is free." >&2
        fi
        launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST"
        echo ">> service $LABEL loaded — the app starts at login and restarts on crash"
        echo ">>   UI:  http://localhost:8080  (give it a few seconds on first start)"
    else
        echo ">> --no-load: $PLIST written, service not loaded"
    fi
    echo ">>   app log:   $LOG_DIR/app.log"
    echo ">>   restart:   launchctl kickstart -k gui/\$(id -u)/$LABEL"
    echo ">>   stop:      launchctl bootout gui/\$(id -u)/$LABEL"
fi

# --------------------------------------------------------- battery watchdog ---

if [ "$INSTALL_BATTERY" = 1 ]; then
    echo ">> installing the battery/power Telegram watchdog..."
    "$SCRIPT_DIR/battery-monitor/install.sh" ${BATTERY_ARGS+"${BATTERY_ARGS[@]}"}
fi

echo ">> done. Uninstall everything with: $SCRIPT_DIR/uninstall.sh [--purge]"
