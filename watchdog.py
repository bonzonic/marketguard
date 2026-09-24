"""Notice that capture has stopped, and do something about it.

Task Scheduler's restart-on-failure catches a dead process. It does not catch
the failure that actually costs us data: a websocket that half-opens. The TCP
connection stays ESTABLISHED, no FIN, no RST, nothing ever arrives, and the
recorder sits forever on `async for raw in ws` - alive, connected, healthy by
every process-level measure, and writing nothing. The library's 20s ping
should notice. "Should" is not a monitoring strategy for five unattended
weeks.

So liveness here is measured from the archive: how old is the newest record we
can actually read back (status.capture_age_s). That is the only signal that
distinguishes "the market is quiet" from "we stopped listening", and it
catches every other cause too - dead process, wrong data directory, full disk,
a second recorder fighting over the hour - without having to enumerate them.

What it does about it, in escalating order:

    task not running        start it
    data stale              stop the task and start it again, and say so
    still stale after two   stop restarting, keep alerting - a restart loop
      restarts in an hour   turns one bad hour into a shredded archive of
                            hundreds of tiny files
    disk getting low        alert well before the recorder's own 5 GB abort,
                            because the abort is silent and this is not

Run every five minutes by \\MarketGuard\\Watchdog. Exits non-zero when
something is wrong so the task's Last Run Result carries the same signal.
"""
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import config
import notify
import status

# Alert here rather than at the recorder's own 5 GB abort. Measured burn is
# ~0.08 GB/day across six symbols - about 3.3 GB for a 40-day run - and even a
# fivefold volatility spike only makes that ~0.4 GB/day. So 25 GB is somewhere
# between two months and a year of headroom: the alert is never about the
# recording outgrowing the disk, it is about something *else* filling the
# drive, which happens fast and without warning.
WARN_FREE_GB = 25.0
CRIT_FREE_GB = 8.0

# Long enough that a restart has a chance to take hold and produce data before
# we conclude it did not work. The recorder needs a websocket handshake plus a
# first flush, so a handful of seconds in practice.
RESTART_COOLDOWN_S = 900

# Two restarts inside this window means restarting is not the answer. Every
# restart opens a new file (recorder never appends to an existing one), so a
# tight loop would fragment the archive into hundreds of stubs.
RESTART_WINDOW_S = 3600
MAX_RESTARTS_IN_WINDOW = 2

# Do not re-send the same alert every five minutes. The first one is
# information; the twentieth is noise that trains the reader to ignore it.
ALERT_REPEAT_S = 6 * 3600


def _log(msg: str) -> None:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def _state_path() -> Path:
    return config.STATE_DIR / "watchdog.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A corrupt or absent state file must not stop the check running. The
        # cost of losing it is one duplicate alert, not a missed failure.
        return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        _log(f"could not write state: {exc}")


def _alert_once(state: dict, key: str, level: str, title: str, body: str) -> None:
    """Send unless the same key went out recently."""
    sent = state.setdefault("alerts", {})
    now = time.time()
    if now - sent.get(key, 0) < ALERT_REPEAT_S:
        _log(f"{level} {title} (alert suppressed, sent recently)")
        return
    channels = notify.alert(level, title, body)
    sent[key] = now
    _log(f"{level} {title} -> {', '.join(channels) or 'NOTHING'}")


def _clear_alert(state: dict, key: str) -> None:
    """Forget a resolved condition so its recurrence alerts immediately."""
    state.setdefault("alerts", {}).pop(key, None)


def _task(verb: str, name: str) -> bool:
    task = f"{status.TASK_FOLDER}\\{name}"
    script = f"{verb}-ScheduledTask -TaskPath '{status.TASK_FOLDER}\\' -TaskName '{name}'"
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"{verb} {task} failed: {exc}")
        return False
    if done.returncode != 0:
        _log(f"{verb} {task} failed: {done.stderr.strip()[:200]}")
    return done.returncode == 0


# Stop-ScheduledTask is not enough, and this was measured rather than assumed:
# it ends the task's action process but leaves the Python process running. The
# action is powershell -> cmd (for output redirection) -> python, and only the
# top of that chain is terminated. Starting the task again then gives two
# recorders writing the same hour.
#
# So the restart kills by identity instead. Killing every match is safe even
# though a venv's python.exe appears twice: the launcher stub holds its child
# in a job object with kill-on-close, so killing the stub takes the real
# interpreter with it. It is also deliberately indiscriminate - if capture has
# stalled, a recorder somebody left in a console window is part of the problem,
# not something to preserve.
_KILL_SCRIPT = r"""
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*recorder.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
"""


def _kill_recorders() -> None:
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _KILL_SCRIPT],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"could not kill recorder processes: {exc}")


def _restart_recorder(state: dict) -> str:
    """Stop and start the recorder task. Returns what happened, for the alert.

    The stop is a hard terminate - Windows has no SIGTERM to hand a
    console-less process - so the current file ends mid-gzip-block. That is
    survivable by construction: the recorder never appends to an existing file
    on restart, and datafile.read_lines already tolerates a truncated tail. At
    most 15 seconds of buffered data is lost, against hours lost by not
    restarting.
    """
    now = time.time()
    history = [t for t in state.get("restarts", []) if now - t < RESTART_WINDOW_S]

    if now - max(history, default=0) < RESTART_COOLDOWN_S:
        return "already restarted recently, waiting"
    if len(history) >= MAX_RESTARTS_IN_WINDOW:
        return (
            f"{len(history)} restarts in the last hour did not help - not restarting "
            "again. Check the recorder log and the network."
        )

    _task("Stop", status.RECORDER_TASK)
    _kill_recorders()
    time.sleep(3)  # let the processes actually exit before asking for a new one
    started = _task("Start", status.RECORDER_TASK)
    history.append(now)
    state["restarts"] = history
    return "restarted the recorder task" if started else "FAILED to restart the recorder task"


def _heartbeat(state: dict) -> None:
    """Once a day, send the status view.

    Not decoration: a silent monitor and a working one look identical. A daily
    message carrying the real numbers is the only cheap proof that the alert
    path is still capable of reaching a human.
    """
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    if state.get("heartbeat_day") == today:
        return
    lines, healthy = status.report()
    notify.alert(
        "INFO",
        f"MarketGuard daily: {'ok' if healthy else 'NEEDS ATTENTION'}",
        "\n".join(lines),
    )
    state["heartbeat_day"] = today


def check() -> int:
    data_dir = config.DATA_DIR
    state = _load_state()
    problems = 0

    _log(f"checking {data_dir}")

    # --- disk ------------------------------------------------------------
    free = status.disk_free_gb(data_dir)
    if free < CRIT_FREE_GB:
        problems += 1
        _alert_once(
            state, "disk", "CRIT",
            f"MarketGuard: {free:.1f} GB free",
            f"The recorder stops recording below 5 GB and will not restart itself. "
            f"Free space on {data_dir.drive} now.",
        )
    elif free < WARN_FREE_GB:
        problems += 1
        _alert_once(
            state, "disk", "WARN",
            f"MarketGuard: disk down to {free:.1f} GB free",
            f"Recording stops below 5 GB. Nothing here is growing fast enough to "
            f"cause this, so something else is filling {data_dir.drive}.",
        )
    else:
        _clear_alert(state, "disk")
    _log(f"disk: {free:.1f} GB free")

    # --- is the task even running ----------------------------------------
    tasks = status.task_states()
    recorder_state = tasks.get(status.RECORDER_TASK)
    if recorder_state is None:
        problems += 1
        _alert_once(
            state, "task-missing", "CRIT",
            "MarketGuard: the recorder task is not installed",
            "Nothing will restart the recorder after a reboot. "
            "Run ops/install-tasks.ps1 as Administrator.",
        )
    elif recorder_state != "Running":
        _log(f"recorder task is {recorder_state} - starting it")
        _task("Start", status.RECORDER_TASK)
    else:
        _clear_alert(state, "task-missing")

    # --- is data actually arriving ---------------------------------------
    age = status.capture_age_s(data_dir)
    if age is None:
        problems += 1
        action = _restart_recorder(state)
        _alert_once(
            state, "stale", "CRIT",
            "MarketGuard: no readable records at all",
            f"Nothing could be read from {data_dir}. Action taken: {action}.",
        )
    elif age > status.STALE_SECONDS:
        problems += 1
        action = _restart_recorder(state)
        _alert_once(
            state, "stale", "CRIT",
            f"MarketGuard: capture stalled, newest record {age / 60:.0f}m old",
            "The recorder process may still be alive - a half-open websocket "
            f"looks exactly like this. Action taken: {action}.",
        )
        _log(f"STALE: {age:.0f}s - {action}")
    else:
        _clear_alert(state, "stale")
        _log(f"capture live: newest record {age:.0f}s old")

    _heartbeat(state)
    state["last_check"] = time.time()
    _save_state(state)
    return problems


def main() -> int:
    if "--dry-run" in sys.argv:
        # Everything except acting on what it finds, for checking the wiring
        # against a live recorder you do not want restarted.
        global _restart_recorder, _task
        _restart_recorder = lambda _state: "dry run, no restart"  # noqa: E731
        _task = lambda verb, name: _log(f"dry run: would {verb} {name}") or True  # noqa: E731
        notify.alert = lambda level, title, body: _log(f"dry run: would alert {level} {title}") or ["dry-run"]  # noqa: E731
    problems = check()
    _log("ok" if problems == 0 else f"{problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
