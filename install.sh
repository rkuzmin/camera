#!/usr/bin/env bash
# Install the camera app as a service — launchd on macOS, systemd on Linux —
# together with the battery/power Telegram watchdog from battery-monitor/.
#
#   camera app       — app.py under the repo venv; starts at login (macOS) or
#                      at boot (Linux system units), restarts if it crashes
#   battery watchdog — pings a Telegram bot on power outage / low battery
#
# If the venv is missing it is created (.venv) and requirements installed.
# On Linux the default is system-wide units (asks for sudo once) so services
# run from boot with no login; --user-service installs user units instead
# (then enable lingering: sudo loginctl enable-linger <user>).
#
# Usage: ./install.sh [options]
#   --dir <path>          app directory to run from (default: this repo)
#   --app-only            install only the camera app service
#   --battery-only        install only the battery watchdog
#   --user-service        Linux: systemd user units instead of system-wide
#   --no-load             write files but don't register/start the services
#   battery watchdog options are passed through: --token, --chat-id,
#   --thresholds, --interval, --lang en|ru, --no-power-events, --no-test
#   (without --token/--chat-id the watchdog installer asks interactively;
#    see battery-monitor/README.md for creating the bot)
#
# Manage later —
#   macOS:  launchctl bootout gui/$(id -u)/local.camera-app     (stop)
#           launchctl kickstart -k gui/$(id -u)/local.camera-app (restart)
#   Linux:  sudo systemctl {status,restart,stop} camera-app
#           (user units: systemctl --user ...)

set -euo pipefail

LABEL="local.camera-app"    # launchd label (macOS)
UNIT="camera-app"           # systemd unit name (Linux)
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENTS_DIR/$LABEL.plist"
LOG_DIR="$HOME/.local/state/camera-app"
SYS_UNIT_DIR="/etc/systemd/system"
USER_UNIT_DIR="$HOME/.config/systemd/user"
OS="${OS_OVERRIDE:-$(uname -s)}"
USER_NAME=$(id -un)

APP_DIR="$SCRIPT_DIR"
INSTALL_APP=1
INSTALL_BATTERY=1
LOAD=1
SCOPE="system"   # Linux only: system | user
BATTERY_ARGS=()

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "install.sh: $*" >&2; exit 1; }
need_arg() { [ $# -ge 2 ] || die "option $1 needs a value"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dir)             need_arg "$@"; APP_DIR=$(cd "$2" && pwd) || die "bad --dir"; shift 2 ;;
        --app-only)        INSTALL_BATTERY=0; shift ;;
        --battery-only)    INSTALL_APP=0; shift ;;
        --user-service)    SCOPE="user"; BATTERY_ARGS+=(--user-service); shift ;;
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

case "$OS" in
    Darwin) ;;
    Linux)
        if [ "$LOAD" = 1 ]; then
            command -v systemctl >/dev/null 2>&1 || die "systemctl not found — this installer needs systemd"
        elif [ "$SCOPE" = "system" ]; then
            SCOPE="user"   # --no-load writes plain files only; system scope would need sudo
            echo ">> --no-load: writing user-scope units (no sudo)" >&2
        fi
        ;;
    *) die "unsupported OS: $OS (macOS and Linux only)" ;;
esac

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
        command -v python3 >/dev/null 2>&1 \
            || die "python3 not found — install it first (macOS: xcode-select --install, Ubuntu: sudo apt install python3)"
        echo ">> no venv found — creating $APP_DIR/.venv and installing requirements"
        echo ">>   (first install downloads OpenCV/ultralytics — this can take a while)"
        python3 -m venv "$APP_DIR/.venv" \
            || die "venv creation failed — on Ubuntu/Debian run: sudo apt install python3-venv"
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
    if [ "$OS" = "Linux" ] && ! "$PY" -c 'import cv2' >/dev/null 2>&1; then
        die "OpenCV fails to import — on a headless Ubuntu this usually needs:
  sudo apt install libgl1 libglib2.0-0
(libglib2.0-0t64 on Ubuntu 24.04+), then re-run ./install.sh"
    fi

    if [ "$LOAD" = 1 ] && command -v lsof >/dev/null 2>&1 \
        && lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
        echo "warning: something already listens on port 8080 (the app started by hand?)." >&2
        echo "warning: stop it, or the service will crash-loop until the port is free." >&2
    fi

    if [ "$OS" = "Darwin" ]; then
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
            launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
            launchctl bootstrap "gui/$(id -u)" "$PLIST"
            echo ">> service $LABEL loaded — the app starts at login and restarts on crash"
        else
            echo ">> --no-load: $PLIST written, service not loaded"
        fi
        echo ">>   UI:      http://localhost:8080  (give it a few seconds on first start)"
        echo ">>   app log: $LOG_DIR/app.log"
        echo ">>   restart: launchctl kickstart -k gui/\$(id -u)/$LABEL"
        echo ">>   stop:    launchctl bootout gui/\$(id -u)/$LABEL"
    else
        emit_app_unit() {  # $1 = system|user
            cat <<EOF
[Unit]
Description=Camera viewer/recorder (app.py)
After=network-online.target
Wants=network-online.target

[Service]
EOF
            if [ "$1" = "system" ]; then
                printf 'User=%s\n' "$USER_NAME"
                printf 'Environment=HOME=%s\n' "$HOME"
            fi
            printf 'WorkingDirectory=%s\n' "$APP_DIR"
            printf 'ExecStart="%s" "%s/app.py"\n' "$PY" "$APP_DIR"
            cat <<EOF
Restart=always
RestartSec=10

[Install]
EOF
            if [ "$1" = "system" ]; then
                echo "WantedBy=multi-user.target"
            else
                echo "WantedBy=default.target"
            fi
        }

        if [ "$SCOPE" = "system" ] && [ "$LOAD" = 1 ]; then
            if ! sudo -v; then
                echo "warning: no sudo — falling back to user units; for 24/7 operation run:" >&2
                echo "warning:   sudo loginctl enable-linger $USER_NAME" >&2
                SCOPE="user"
            fi
        fi

        if [ "$SCOPE" = "system" ]; then
            emit_app_unit system | sudo tee "$SYS_UNIT_DIR/$UNIT.service" >/dev/null
            sudo systemctl daemon-reload
            sudo systemctl enable "$UNIT.service" >/dev/null
            sudo systemctl restart "$UNIT.service"
            echo ">> system service $UNIT enabled — starts at boot, restarts on crash"
            echo ">>   UI:      http://<server-ip>:8080  (give it a few seconds on first start)"
            echo ">>   logs:    sudo journalctl -u $UNIT -f"
            echo ">>   manage:  sudo systemctl {status,restart,stop} $UNIT"
        else
            mkdir -p "$USER_UNIT_DIR"
            emit_app_unit user > "$USER_UNIT_DIR/$UNIT.service"
            if [ "$LOAD" = 1 ]; then
                systemctl --user daemon-reload
                systemctl --user enable "$UNIT.service" >/dev/null
                systemctl --user restart "$UNIT.service"
                echo ">> user service $UNIT enabled"
                echo ">>   UI:      http://<server-ip>:8080"
                echo ">>   logs:    journalctl --user-unit $UNIT -f"
                echo ">>   manage:  systemctl --user {status,restart,stop} $UNIT"
                if command -v loginctl >/dev/null 2>&1; then
                    if [ "$(loginctl show-user "$USER_NAME" --property=Linger --value 2>/dev/null || true)" != "yes" ]; then
                        echo "warning: user services stop when you log out; for 24/7 operation run:" >&2
                        echo "warning:   sudo loginctl enable-linger $USER_NAME" >&2
                    fi
                fi
            else
                echo ">> --no-load: $USER_UNIT_DIR/$UNIT.service written, not enabled"
            fi
        fi
    fi
fi

# --------------------------------------------------------- battery watchdog ---

if [ "$INSTALL_BATTERY" = 1 ]; then
    echo ">> installing the battery/power Telegram watchdog..."
    "$SCRIPT_DIR/battery-monitor/install.sh" ${BATTERY_ARGS+"${BATTERY_ARGS[@]}"}
fi

echo ">> done. Uninstall everything with: $SCRIPT_DIR/uninstall.sh [--purge]"
