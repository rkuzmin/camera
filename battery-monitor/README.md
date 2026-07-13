# battery-monitor — power/battery alerts to Telegram (macOS / Linux)

A small service for the machine that runs the camera app: every minute it
checks the power state (`pmset` on macOS, `/sys/class/power_supply` on
Linux) and messages a Telegram bot when

- the machine **switches to battery power** — for an always-plugged-in
  camera machine this usually means a power outage;
- the battery **crosses a low threshold** while discharging (default 20%,
  10%, 5% — one alert per threshold per discharge cycle, no spam);
- **AC power comes back**.

Runs from launchd on macOS and from a systemd timer on Linux. No
dependencies beyond `bash` and `curl`.

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
`~/.local/bin/battery-monitor`, registers the service — the LaunchAgent
`local.battery-monitor` on macOS, or `battery-monitor.service` +
`battery-monitor.timer` on Linux (system-wide via one sudo by default so it
runs from boot; `--user-service` for user units + `sudo loginctl
enable-linger $USER`) — starts it, and sends a test message to the bot.

Options: `--thresholds "20 10 5"`, `--interval <sec>`, `--lang en|ru`,
`--no-power-events`, `--proxy <url>`, `--user-service`, `--no-test`,
`--no-load`. Re-run `install.sh` any time to change settings — it updates
everything in place.

## Telegram is blocked on the server (e.g. hosting in Russia)

`api.telegram.org` is blocked at the network level by some Russian ISPs and
hosting providers — `curl` will time out or fail to resolve/connect, even
though the bot token and chat id are correct. Fix: route the watchdog's
requests through a proxy that has unblocked access.

At install time:

```bash
./install.sh --proxy 'socks5h://127.0.0.1:1080'   # or http://user:pass@host:port
```

Already installed? No need to reinstall — just add the line to the config
(it's re-read on every check):

```bash
echo "TELEGRAM_PROXY='socks5h://127.0.0.1:1080'" >> ~/.config/battery-monitor/config
battery-monitor test
```

Prefer `socks5h://` over `socks5://` for a SOCKS5 proxy — the `h` makes the
*proxy* resolve `api.telegram.org`, which matters when DNS for that host is
also poisoned/blocked locally, not just the connection. For an HTTP/HTTPS
proxy (`http://...`), DNS resolution always happens on the proxy side, so
plain `http://` is fine. Provisioning the proxy itself (a SOCKS5/Shadowsocks
endpoint on a host outside the block, or an existing one you already use) is
outside this script's scope.

## Files

| Path | What |
|---|---|
| `~/.local/bin/battery-monitor` | the monitor script (`check` / `test` / `status`) |
| `~/.config/battery-monitor/config` | settings incl. bot token (`chmod 600`, never in the repo) |
| `~/Library/LaunchAgents/local.battery-monitor.plist` | the service (macOS) |
| `/etc/systemd/system/battery-monitor.{service,timer}` | the service (Linux; user units go to `~/.config/systemd/user/`) |
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
launchctl print gui/$(id -u)/local.battery-monitor      # macOS: launchd's view
systemctl status battery-monitor.timer                  # Linux: timer state
```

## Uninstall

```bash
./uninstall.sh          # stop + remove service and binary, keep config/logs
./uninstall.sh --purge  # remove everything incl. the bot token config
```
