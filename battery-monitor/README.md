# battery-monitor — power/battery alerts to Telegram (macOS)

A small launchd service for the Mac that runs the camera app: every minute it
checks the power state with `pmset` and messages a Telegram bot when

- the Mac **switches to battery power** — for an always-plugged-in camera
  machine this usually means a power outage;
- the battery **crosses a low threshold** while discharging (default 20%,
  10%, 5% — one alert per threshold per discharge cycle, no spam);
- **AC power comes back**.

No dependencies beyond what ships with macOS (`bash`, `curl`, `pmset`,
`launchd`).

## 1. Create a bot and get the two values

1. In Telegram, talk to [@BotFather](https://t.me/BotFather) → `/newbot` →
   copy the **bot token** (looks like `<digits>:<long secret>`).
2. Open your new bot's chat and press **Start** (the bot cannot message you
   first).
3. Get your **chat id**: message [@userinfobot](https://t.me/userinfobot),
   or send your bot any message and open
   `https://api.telegram.org/bot<your-bot-token>/getUpdates` in a browser —
   the id is in `"chat":{"id":...}`.

## 2. Install

The repo-root `./install.sh` installs this watchdog together with the camera
app service and forwards all the flags below. Standalone:

```bash
cd battery-monitor
./install.sh                # asks for the token and chat id
```

or non-interactively:

```bash
./install.sh --token '<your-bot-token>' --chat-id '<your-chat-id>' --lang ru
```

The installer writes the config, installs the script as
`~/.local/bin/battery-monitor`, creates the LaunchAgent
`local.battery-monitor` (checks every 60 s, starts on login), loads it, and
sends a test message to the bot.

Options: `--thresholds "20 10 5"`, `--interval <sec>`, `--lang en|ru`,
`--no-power-events`, `--no-test`, `--no-load`. Re-run `install.sh` any time to
change settings — it updates everything in place.

## Files

| Path | What |
|---|---|
| `~/.local/bin/battery-monitor` | the monitor script (`check` / `test` / `status`) |
| `~/.config/battery-monitor/config` | settings incl. bot token (`chmod 600`, never in the repo) |
| `~/Library/LaunchAgents/local.battery-monitor.plist` | the launchd service |
| `~/.local/state/battery-monitor/monitor.log` | log (size-capped) |
| `~/.local/state/battery-monitor/state` | alert de-duplication state |

The config can be edited directly (thresholds, language, `HOST_LABEL`,
`DRY_RUN=1` for testing) — it is re-read on every check, no reload needed.
Changing `--interval` requires re-running `install.sh`.

## Check that it works

```bash
battery-monitor status                                  # parsed battery state
battery-monitor test                                    # send a test message
tail -f ~/.local/state/battery-monitor/monitor.log      # one line per check
launchctl print gui/$(id -u)/local.battery-monitor      # launchd's view
```

## Uninstall

```bash
./uninstall.sh          # stop + remove service and binary, keep config/logs
./uninstall.sh --purge  # remove everything incl. the bot token config
```
