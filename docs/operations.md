# Operations: keeping the bot running

There are two ways to run the bot unattended:

- **Option 1 (recommended): one long-running process**, `uv run trader run`, kept alive by
  the operating system's service manager. It refreshes at `schedule.refresh_time` (16:25) on
  trading days and sends at `schedule.send_time` (07:00) on `schedule.send_on_days`, both in
  the exchange timezone. Each job runs at most once per local date even across restarts, and
  a run missed during sleep is made up on wake within the misfire grace (6 h refresh, 3 h
  send). A job failing with a transient error (network not yet up after wake, SMTP 4xx,
  Alpaca 5xx, locked database) is retried every 5 minutes within the same grace. A job left
  `running` by a crash is not skipped: after a restart the scheduler waits until the claim is
  15 minutes old and then re-runs the job on the next tick, as long as that is still within
  the grace, so the morning email is still sent once.
- **Option 2: two one-shot commands** from cron or Task Scheduler:
  `uv run trader daily` after the close and `uv run trader send --latest` in the morning.

Pick one, not both, for a given data directory.

## Before you start

- Run everything from the repository directory (that is where `.env` is read from) or set
  the variables in the service definition. Use absolute paths in service files: find `uv`
  with `which uv` (Linux, macOS) or `where uv` (Windows).
- Check the setup interactively first: `uv run trader check-config` then
  `uv run trader daily --dry-run`.
- **The machine must be awake** at the scheduled times, or wake within the misfire grace.
  A sleeping laptop does not run jobs; on wake, `trader run` catches up if within grace and
  otherwise logs the miss. cron does not catch up at all (anacron and Task Scheduler's
  "run as soon as possible after a missed start" do). Disable sleep or schedule a wake
  (`pmset repeat wakeorpoweron` on macOS, "Wake the computer to run this task" on Windows,
  `rtcwake` on Linux) if you rely on the 07:00 email.
- **Timezones.** All schedule times are exchange-local (`UT_TIMEZONE`, default
  `America/New_York`) and converted with `zoneinfo`, so DST changes are handled and the
  machine's own timezone does not matter for `trader run`. The IANA database comes from the
  `tzdata` dependency, which matters on Windows (no system database); keep it current with
  `uv sync` so new DST rules are picked up. cron and Task Scheduler, however, fire in the
  **machine's** local time: if the machine is not on US Eastern time, convert 16:30 and
  07:00 Eastern yourself (and note the conversion shifts when only one side observes DST).
- Keep `UT_DATA_DIR` on a local disk, never on a cloud-synced folder (SQLite WAL files).
- Logs go to standard error; the examples below capture them.

## Option 1: `trader run` as a service

### Linux: systemd user unit

`~/.config/systemd/user/uncorrelated-trading.service`:

```ini
[Unit]
Description=uncorrelated-trading advisory scheduler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/uncorrelated-trading
ExecStart=%h/.local/bin/uv run trader run
Restart=on-failure
RestartSec=60
# systemd sends SIGTERM on stop; the scheduler exits cleanly.
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now uncorrelated-trading.service
journalctl --user -u uncorrelated-trading -f      # follow the logs
loginctl enable-linger "$USER"                    # keep running when you are logged out
```

### macOS: launchd agent

`~/Library/LaunchAgents/com.example.uncorrelated-trading.plist` (replace `you` and paths):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.uncorrelated-trading</string>
  <key>WorkingDirectory</key>
  <string>/Users/you/uncorrelated-trading</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/.local/bin/uv</string>
    <string>run</string>
    <string>trader</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/you/Library/Logs/uncorrelated-trading.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/you/Library/Logs/uncorrelated-trading.err.log</string>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.uncorrelated-trading.plist
launchctl kickstart -k gui/$(id -u)/com.example.uncorrelated-trading     # restart
launchctl bootout gui/$(id -u)/com.example.uncorrelated-trading          # stop and unload
```

### Windows: Task Scheduler

Start `trader run` at logon and restart it if it stops (PowerShell or `cmd`, one line;
adjust the paths):

```bat
schtasks /Create /TN "uncorrelated-trading" /SC ONLOGON /RL LIMITED /F ^
  /TR "cmd /c cd /d C:\Users\you\uncorrelated-trading && C:\Users\you\.local\bin\uv.exe run trader run >> data\trader.log 2>&1"
```

Then, in the Task Scheduler GUI, open the task's *Settings* tab and tick *If the task
fails, restart every 1 minute* and untick *Stop the task if it runs longer than 3 days*; on
the *Conditions* tab untick *Start the task only if the computer is on AC power* for a
laptop. `schtasks /Run /TN "uncorrelated-trading"` starts it immediately and
`schtasks /End /TN "uncorrelated-trading"` stops it.

## Option 2: one-shot commands

Run `daily` at 16:30 exchange time on weekdays (it analyses today once the close has
settled; on holidays it resolves to the previous trading day) and `send --latest` at 07:00.
If the previous evening's `daily` already delivered the email, the morning `send` exits 5
("already sent"), which is harmless; to get only the morning email, use
`daily --dry-run` in the evening instead (`--dry-run` writes the outbox copy and does not
mark the run as sent). Plain `uv run trader daily` at 16:30 alone is also fine if you
prefer the email in the evening. On exchange holidays the weekday schedule still fires and
re-analyses the previous trading day, so that day's brief is sent again.

### cron (Linux, macOS)

`crontab -e`, machine clock on US Eastern time (otherwise add `CRON_TZ=America/New_York`
where your cron supports it, as cronie does):

```cron
CRON_TZ=America/New_York
30 16 * * 1-5  cd $HOME/uncorrelated-trading && $HOME/.local/bin/uv run trader daily --dry-run >> data/cron.log 2>&1
0 7 * * 1-5    cd $HOME/uncorrelated-trading && $HOME/.local/bin/uv run trader send --latest >> data/cron.log 2>&1
```

### Windows Task Scheduler

Times are the machine's local time:

```bat
schtasks /Create /TN "uncorrelated-trading daily" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 16:30 /F ^
  /TR "cmd /c cd /d C:\Users\you\uncorrelated-trading && C:\Users\you\.local\bin\uv.exe run trader daily --dry-run >> data\daily.log 2>&1"
schtasks /Create /TN "uncorrelated-trading send" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 07:00 /F ^
  /TR "cmd /c cd /d C:\Users\you\uncorrelated-trading && C:\Users\you\.local\bin\uv.exe run trader send --latest >> data\send.log 2>&1"
```

In each task's *Settings* tab tick *Run task as soon as possible after a scheduled start is
missed*.

## Exit codes for monitoring

0 ok, 1 unexpected error, 2 configuration or usage error, 3 data or storage error,
4 analysis error, 5 report or email error (including "already sent"). Codes 3 and 5 are
often transient (network, rate limits) and safe to retry; 2 needs a human.

## Maintenance

- Update: `git pull && uv sync --all-extras`, then restart the service.
- Back up `UT_DATA_DIR/uncorrelated_trading.sqlite3` while the bot is stopped. If it is lost,
  the next run backfills the bars again (only run history and send records are lost).
- Rotate the log files written by the cron and Task Scheduler examples occasionally.
