#!/usr/bin/env bash
# battery-monitor — watch this Mac's power/battery state and alert a Telegram
# bot when the machine switches to battery power (possible outage), crosses
# low-battery thresholds while discharging, or gets AC power back.
#
# One check per invocation; launchd runs it periodically (see install.sh).
# macOS only (reads state with `pmset`).
#
# Usage: battery-monitor [check|test|status]
#   check   read battery state and send alerts if needed (default, used by launchd)
#   test    send a test message to the configured Telegram chat
#   status  print parsed battery state, config summary and alert state
#
# Config: ~/.config/battery-monitor/config (written by install.sh); override
# the path with BATTERY_MONITOR_CONFIG. State + log live in
# ~/.local/state/battery-monitor; override with BATTERY_MONITOR_STATE_DIR.

set -euo pipefail

CONFIG_FILE="${BATTERY_MONITOR_CONFIG:-$HOME/.config/battery-monitor/config}"
STATE_DIR="${BATTERY_MONITOR_STATE_DIR:-$HOME/.local/state/battery-monitor}"
STATE_FILE="$STATE_DIR/state"
LOG_FILE="$STATE_DIR/monitor.log"
MAX_LOG_BYTES=1000000

# Defaults; the config file overrides them.
TELEGRAM_BOT_TOKEN=""
TELEGRAM_CHAT_ID=""
ALERT_THRESHOLDS="20 10 5"   # battery % levels that trigger a low-battery alert
NOTIFY_POWER_EVENTS=1        # 1 = alert on AC <-> battery transitions
MESSAGE_LANG="en"            # en | ru
HOST_LABEL=""                # empty = use the Mac's computer name
DRY_RUN=0                    # 1 = log messages instead of sending them
# shellcheck source=/dev/null
[ -f "$CONFIG_FILE" ] && . "$CONFIG_FILE"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"
    [ -t 1 ] && printf '%s\n' "$*"
    return 0
}

trim_log() {
    [ -f "$LOG_FILE" ] || return 0
    local size
    size=$(wc -c <"$LOG_FILE")
    if [ "$size" -gt "$MAX_LOG_BYTES" ]; then
        tail -n 2000 "$LOG_FILE" >"$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
    fi
}

# ---------------------------------------------------------------- battery ---

SOURCE="UNKNOWN"   # AC | BATTERY | UNKNOWN
PERCENT=""         # empty = no battery found (desktop Mac)
CHARGE_STATE=""    # discharging | charging | charged | ...
REMAIN=""          # h:mm estimate while discharging, may be empty

read_battery() {
    local out line
    out=$(pmset -g batt)
    case "$out" in
        *"'AC Power'"*)      SOURCE="AC" ;;
        *"'Battery Power'"*) SOURCE="BATTERY" ;;
        *)                   SOURCE="UNKNOWN" ;;
    esac
    line=$(printf '%s\n' "$out" | grep -i 'InternalBattery' | head -n 1 || true)
    [ -n "$line" ] || return 0
    PERCENT=$(printf '%s\n' "$line" | grep -Eo '[0-9]+%' | head -n 1 | tr -d '%' || true)
    CHARGE_STATE=$(printf '%s\n' "$line" | sed -nE 's/.*%; *([^;]+);.*/\1/p')
    REMAIN=$(printf '%s\n' "$line" | grep -Eo '[0-9]+:[0-9]{2}' | head -n 1 || true)
    if [ "$REMAIN" = "0:00" ]; then REMAIN=""; fi
    return 0
}

# ------------------------------------------------------------------ state ---

LAST_SOURCE=""        # power source seen on the previous check
ALERTED_THRESHOLD=""  # lowest threshold already alerted this discharge cycle

load_state() {
    # shellcheck source=/dev/null
    [ -f "$STATE_FILE" ] && . "$STATE_FILE"
    return 0
}

save_state() {
    {
        printf 'LAST_SOURCE=%q\n' "$LAST_SOURCE"
        printf 'ALERTED_THRESHOLD=%q\n' "$ALERTED_THRESHOLD"
    } >"$STATE_FILE.tmp"
    mv "$STATE_FILE.tmp" "$STATE_FILE"
}

# --------------------------------------------------------------- messages ---

host_label() {
    if [ -n "$HOST_LABEL" ]; then
        printf '%s' "$HOST_LABEL"
    else
        scutil --get ComputerName 2>/dev/null || hostname -s
    fi
}

remain_suffix() {
    [ -n "$REMAIN" ] || return 0
    if [ "$MESSAGE_LANG" = "ru" ]; then
        printf ' (осталось ~%s)' "$REMAIN"
    else
        printf ' (~%s left)' "$REMAIN"
    fi
}

source_desc() {
    if [ "$MESSAGE_LANG" = "ru" ]; then
        case "$SOURCE" in
            AC)      printf 'питание от сети' ;;
            BATTERY) printf 'питание от батареи' ;;
            *)       printf 'источник питания неизвестен' ;;
        esac
    else
        case "$SOURCE" in
            AC)      printf 'on AC power' ;;
            BATTERY) printf 'on battery power' ;;
            *)       printf 'power source unknown' ;;
        esac
    fi
}

msg_power_lost() {
    if [ "$MESSAGE_LANG" = "ru" ]; then
        printf '⚡️ %s перешёл на батарею — %s%%%s. Возможно, пропало электричество.' \
            "$(host_label)" "$PERCENT" "$(remain_suffix)"
    else
        printf '⚡️ %s switched to battery power — %s%%%s. Possible power outage.' \
            "$(host_label)" "$PERCENT" "$(remain_suffix)"
    fi
}

msg_power_restored() {
    if [ "$MESSAGE_LANG" = "ru" ]; then
        printf '🔌 %s снова питается от сети — %s%%.' "$(host_label)" "$PERCENT"
    else
        printf '🔌 %s is back on AC power — %s%%.' "$(host_label)" "$PERCENT"
    fi
}

msg_low() {
    local icon="🪫"
    if [ "$PERCENT" -le 10 ]; then icon="🚨"; fi
    if [ "$MESSAGE_LANG" = "ru" ]; then
        printf '%s %s: батарея разряжена до %s%%%s. Подключите зарядку.' \
            "$icon" "$(host_label)" "$PERCENT" "$(remain_suffix)"
    else
        printf '%s %s: battery down to %s%%%s and discharging. Plug in the charger.' \
            "$icon" "$(host_label)" "$PERCENT" "$(remain_suffix)"
    fi
}

msg_test() {
    local batt
    if [ -n "$PERCENT" ]; then
        batt="${PERCENT}%, $(source_desc)"
    elif [ "$MESSAGE_LANG" = "ru" ]; then
        batt="батарея не обнаружена"
    else
        batt="no battery detected"
    fi
    if [ "$MESSAGE_LANG" = "ru" ]; then
        printf '✅ Мониторинг батареи работает на «%s» — %s.' "$(host_label)" "$batt"
    else
        printf '✅ Battery monitor is active on "%s" — %s.' "$(host_label)" "$batt"
    fi
}

# --------------------------------------------------------------- telegram ---

send_telegram() {
    local text="$1"
    if [ "$DRY_RUN" = "1" ]; then
        log "dry-run, would send: $text"
        return 0
    fi
    if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
        log "ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — edit $CONFIG_FILE"
        return 1
    fi
    if curl -fsS --max-time 15 --retry 2 --retry-delay 2 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${text}" >/dev/null 2>>"$LOG_FILE"; then
        log "sent: $text"
    else
        log "ERROR: failed to send Telegram message: $text"
        return 1
    fi
}

# --------------------------------------------------------------- commands ---

do_check() {
    read_battery
    load_state
    local prev_source="$LAST_SOURCE" prev_alert="$ALERTED_THRESHOLD"
    local new_source="$SOURCE" new_alert="$ALERTED_THRESHOLD"

    log "check: source=$SOURCE percent=${PERCENT:-n/a} state=${CHARGE_STATE:-n/a} remain=${REMAIN:-n/a}"

    if [ -z "$PERCENT" ]; then
        # No battery (desktop Mac) — nothing to alert on.
        LAST_SOURCE="$new_source"
        save_state
        return 0
    fi

    # AC <-> battery transitions. On send failure keep the previous source so
    # the transition is re-detected and the alert retried on the next check.
    if [ "$NOTIFY_POWER_EVENTS" = "1" ] && [ -n "$prev_source" ] \
        && [ "$SOURCE" != "$prev_source" ] && [ "$SOURCE" != "UNKNOWN" ]; then
        if [ "$SOURCE" = "BATTERY" ]; then
            send_telegram "$(msg_power_lost)" || new_source="$prev_source"
        else
            send_telegram "$(msg_power_restored)" || new_source="$prev_source"
        fi
    fi

    if [ "$SOURCE" = "AC" ]; then
        new_alert=""   # back on power — re-arm the low-battery alerts
    elif [ "$SOURCE" = "BATTERY" ]; then
        # Deepest threshold the current level has crossed (thresholds may be
        # listed in any order); alert once per threshold per discharge cycle.
        local deepest="" t
        for t in $ALERT_THRESHOLDS; do
            case "$t" in ''|*[!0-9]*) continue ;; esac
            if [ "$PERCENT" -le "$t" ] && { [ -z "$deepest" ] || [ "$t" -lt "$deepest" ]; }; then
                deepest="$t"
            fi
        done
        if [ -n "$deepest" ] && { [ -z "$prev_alert" ] || [ "$deepest" -lt "$prev_alert" ]; }; then
            if send_telegram "$(msg_low)"; then new_alert="$deepest"; fi
        fi
    fi

    LAST_SOURCE="$new_source"
    ALERTED_THRESHOLD="$new_alert"
    save_state
}

do_test() {
    read_battery
    send_telegram "$(msg_test)"
}

do_status() {
    read_battery
    load_state
    echo "config:     $CONFIG_FILE"
    echo "state file: $STATE_FILE (LAST_SOURCE=${LAST_SOURCE:-unset} ALERTED_THRESHOLD=${ALERTED_THRESHOLD:-none})"
    echo "log:        $LOG_FILE"
    echo "battery:    source=$SOURCE percent=${PERCENT:-n/a} state=${CHARGE_STATE:-n/a} remain=${REMAIN:-n/a}"
    echo "settings:   thresholds='$ALERT_THRESHOLDS' power_events=$NOTIFY_POWER_EVENTS lang=$MESSAGE_LANG dry_run=$DRY_RUN"
}

usage() {
    grep '^#' "$0" | sed -n '2,15p' | sed 's/^# \{0,1\}//'
}

main() {
    if ! command -v pmset >/dev/null 2>&1; then
        echo "battery-monitor: pmset not found — this tool is macOS-only" >&2
        exit 1
    fi
    mkdir -p "$STATE_DIR"
    trim_log
    case "${1:-check}" in
        check)          do_check ;;
        test)           do_test ;;
        status)         do_status ;;
        help|-h|--help) usage ;;
        *)              usage >&2; exit 2 ;;
    esac
}

main "$@"
