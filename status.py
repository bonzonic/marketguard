"""One command that answers: is it capturing right now, and has it been?

This gets run dozens of times over a five-week unattended run, almost always
by someone who wants a yes or a no rather than a report. So the first line is
the answer and everything under it is the evidence for that answer.

    python status.py            the answer, plus why
    python status.py --hours 48 widen the coverage strip
    python status.py --log      tail the recorder's own log as well

How "is it capturing" is measured, and why not the obvious way:

`os.stat()` on the file the recorder currently has open is a lie on NTFS. The
directory entry for an open file is updated lazily - we have watched it report
0 bytes for a file holding 6.8 MB, then jump to the truth minutes later. Both
st_size and st_mtime are affected. A freshness check built on either produces
false alarms, and a monitor that cries wolf gets ignored, which is precisely
the failure it existed to prevent.

Reading the file's *contents* goes through the file system cache and is always
current. So capture liveness is the age of the newest record we can actually
read back, which is the only measure that cannot be fooled - and it is also
the measure that catches the failure a process check misses entirely: a
websocket that half-opens, leaving the recorder alive, connected and silent.

This is the at-a-glance view. `coverage.py` is the authoritative audit of what
the archive actually contains.
"""
import json
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import config
import datafile
import notify

# The recorder flushes its gzip buffer every 15s (recorder.FLUSH_INTERVAL_S),
# so the newest readable record is always a little behind reality. 300s is
# twenty times that - past any plausible flush, hour rollover or reconnect,
# and still far short of an hour of lost capture.
STALE_SECONDS = 300

# Below this the headline is still yes, but the number gets shown rather than
# hidden, so a slow drift upward is visible before it becomes a failure.
FRESH_SECONDS = 60

# Ship date. Everything here exists to get the archive intact to this day.
DEADLINE = date(2026, 10, 30)

# An hour is drawn as thin below this fraction of the median hour in the
# window. Loose on purpose: real markets vary several-fold between the Asian
# lull and the US open, and we are hunting order-of-magnitude drops - the
# 11 KB hour that means the machine was asleep - not below-average activity.
THIN_FRACTION = 0.25

HOUR_FMT = "%Y%m%dT%H"

TASK_FOLDER = r"\MarketGuard"
RECORDER_TASK = "Recorder"
WATCHDOG_TASK = "Watchdog"


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------

def capture_age_s(data_dir: Path | None = None) -> float | None:
    """Seconds since the newest readable record, or None if there are none.

    Walks backwards through the last few files rather than reading only the
    newest, because a recorder that restarted or rolled over the hour and then
    stalled leaves a file with nothing yet flushed into it. Trusting only the
    newest file would report that as "no data ever", which is both wrong and
    alarming.
    """
    paths = datafile.data_files(data_dir)
    for path in reversed(paths[-3:]):
        last = None
        for line in datafile.read_lines(path):
            last = line
        if not last:
            continue
        try:
            msg = json.loads(last)
        except json.JSONDecodeError:
            continue
        # The recorder's own receive stamp, not the exchange's event time:
        # this measures whether *we* are still capturing, and depth snapshots
        # carry no exchange timestamp at all.
        stamp = msg.get("t") or datafile.timestamp(msg)
        if stamp:
            return time.time() - stamp / 1000
    return None


def disk_free_gb(path: Path) -> float:
    # Walk up to the first directory that exists - the data dir is created by
    # the recorder on first run and may legitimately not be there yet.
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / 1024**3


def _ps_json(script: str):
    """Run PowerShell, parse its JSON. None on any failure.

    Used instead of parsing schtasks output because schtasks prints localised
    strings and Get-ScheduledTask returns a stable enum.
    """
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _as_list(parsed) -> list:
    """PowerShell's ConvertTo-Json emits a bare object for a single item."""
    if parsed is None:
        return []
    return parsed if isinstance(parsed, list) else [parsed]


def task_states() -> dict[str, str]:
    """Scheduled task name -> state string. Empty if the tasks are not installed."""
    script = (
        f"Get-ScheduledTask -TaskPath '{TASK_FOLDER}\\' -ErrorAction SilentlyContinue | "
        "ForEach-Object { @{ name = $_.TaskName; state = $_.State.ToString() } } | "
        "ConvertTo-Json -Compress"
    )
    return {t["name"]: t["state"] for t in _as_list(_ps_json(script)) if "name" in t}


# Two things make "find the recorder process" less obvious than it looks.
#
# First, a venv's python.exe on Windows is a launcher that re-execs the base
# interpreter, so every recorder appears twice - the stub and its child, both
# matching on command line. Keeping only processes that are not the parent of
# another match collapses that back to one.
#
# Second, the whole point is telling a scheduler-owned recorder from one tied
# to somebody's console window, and the immediate parent cannot answer that:
# run.ps1 launches through cmd.exe for output redirection, so the parent is
# always cmd. The answer is several hops up - Task Scheduler runs actions from
# svchost - so we collect the ancestry and look for it there.
_PROCESS_SCRIPT = r"""
$all = Get-CimInstance Win32_Process
$byId = @{}
foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }
$match = $all | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*recorder.py*' }
$parents = @($match | ForEach-Object { [int]$_.ParentProcessId })
$match | Where-Object { $parents -notcontains [int]$_.ProcessId } | ForEach-Object {
    $chain = @()
    $cur = [int]$_.ParentProcessId
    for ($i = 0; $i -lt 8 -and $byId.ContainsKey($cur); $i++) {
        $chain += $byId[$cur].Name
        $cur = [int]$byId[$cur].ParentProcessId
    }
    @{
        pid   = $_.ProcessId
        age   = [int]((Get-Date) - $_.CreationDate).TotalSeconds
        chain = ($chain -join ' < ')
    }
} | ConvertTo-Json -Compress
"""


def recorder_processes() -> list[dict]:
    """Live recorder processes, each with its ancestry, newest last."""
    procs = _as_list(_ps_json(_PROCESS_SCRIPT))
    return sorted(procs, key=lambda p: p.get("age", 0), reverse=True)


def is_scheduled(proc: dict) -> bool:
    """True if Task Scheduler owns this process rather than a console window."""
    return "svchost" in (proc.get("chain") or "").lower()


def hour_sizes(data_dir: Path | None = None) -> dict[str, int]:
    """Compressed bytes per UTC hour, summed across restart files.

    Summed rather than maxed because a restart mid-hour splits one hour across
    two files and together they cover it. stat() is trustworthy here: every
    file counted is closed. The hour still being written is handled separately
    by capture_age_s, for the reason in the module docstring.
    """
    return {
        hour: sum(p.stat().st_size for p in paths)
        for hour, paths in datafile.files_by_hour(data_dir).items()
    }


def coverage_strip(sizes: dict[str, int], hours: int, live: bool) -> tuple[str, int]:
    """An ASCII strip of the last `hours` UTC hours, plus how many were good.

        #  recorded normally
        -  present but thin - the shape a sleeping machine leaves
        .  no file at all
        >  the hour in progress (judged by capture_age_s, not by size)
    """
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    keys = [(now - timedelta(hours=n)).strftime(HOUR_FMT) for n in range(hours - 1, -1, -1)]

    closed = [sizes[k] for k in keys[:-1] if k in sizes]
    median = sorted(closed)[len(closed) // 2] if closed else 0
    threshold = median * THIN_FRACTION

    out, good = [], 0
    for key in keys:
        if key == keys[-1]:
            out.append(">" if live else ".")
            good += 1 if live else 0
        elif key not in sizes:
            out.append(".")
        elif sizes[key] < threshold:
            out.append("-")
        else:
            out.append("#")
            good += 1
    return "".join(out), good


def burn_rate_gb_per_day(sizes: dict[str, int], days: int = 7) -> float:
    """GB per day of *recorded* hours over the recent past.

    Per recorded hour rather than per elapsed hour: a gap in the archive means
    the machine was down, not that the market was cheaper to store, and
    projecting from elapsed time would understate the disk needed once the gap
    is fixed.
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).strftime(HOUR_FMT)
    current = now.strftime(HOUR_FMT)
    recent = {h: n for h, n in sizes.items() if h >= cutoff and h != current}
    if not recent:
        return 0.0
    return (sum(recent.values()) / len(recent)) * 24 / 1024**3


# --------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------

def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


def _row(label: str, value: str, note: str = "") -> str:
    # Explicit spaces between the columns rather than relying on the padding:
    # the coverage strip and long task names both overrun their width, and a
    # label welded to its value is unreadable exactly when it matters.
    return f"  {label:<14} {value:<21} {note}".rstrip()


def report(hours: int = 24, data_dir: Path | None = None) -> tuple[list[str], bool]:
    """The whole status view as lines, plus whether everything is fine.

    Returned rather than printed so the watchdog can send the same text as its
    daily heartbeat - a heartbeat that carries the real numbers doubles as
    proof that the alert channel still works.
    """
    data_dir = data_dir or config.DATA_DIR
    lines, healthy = [], True

    now = datetime.now().astimezone()
    lines.append(
        f"MarketGuard   {now:%Y-%m-%d %H:%M:%S %z}   "
        f"({datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)"
    )
    lines.append("")

    age = capture_age_s(data_dir)
    if age is None:
        lines.append("  NOT CAPTURING    no readable records in the archive")
        healthy = False
    elif age > STALE_SECONDS:
        lines.append(f"  NOT CAPTURING    newest record is {_duration(age)} old")
        healthy = False
    elif age > FRESH_SECONDS:
        lines.append(f"  CAPTURING        newest record {_duration(age)} old (slow)")
    else:
        lines.append(f"  CAPTURING        newest record {age:.0f}s old")
    lines.append("")

    procs = recorder_processes()
    tasks = task_states()
    if not procs:
        lines.append(_row("recorder", "no process", "nothing is running recorder.py"))
        healthy = False
    for proc in procs:
        if is_scheduled(proc):
            owner = "Task Scheduler"
        else:
            # A recorder whose ancestry runs back to a console window dies
            # with that window - which is the entire problem being fixed here,
            # so it is never a healthy state even though data is arriving.
            owner = f"NOT scheduled - {proc.get('chain', '?')}"
            healthy = False
        lines.append(
            _row("recorder", f"pid {proc['pid']}", f"up {_duration(proc['age'])}, {owner}")
        )

    if tasks:
        for name in (RECORDER_TASK, WATCHDOG_TASK):
            state = tasks.get(name, "NOT INSTALLED")
            lines.append(_row(f"task {name.lower()}", state, f"{TASK_FOLDER}\\{name}"))
            if state not in ("Running", "Ready"):
                healthy = False
    else:
        lines.append(_row("tasks", "NOT INSTALLED", "run ops/install-tasks.ps1 as Administrator"))
        healthy = False

    sizes = hour_sizes(data_dir)
    free = disk_free_gb(data_dir)
    total_gb = sum(sizes.values()) / 1024**3
    per_day = burn_rate_gb_per_day(sizes)
    days_left = max((DEADLINE - date.today()).days, 0)
    need = per_day * days_left

    lines.append(
        _row("disk", f"{free:.1f} GB free", f"{data_dir.drive or data_dir}  (recorder aborts below 5 GB)")
    )
    if sizes:
        span_first = datetime.strptime(min(sizes), HOUR_FMT)
        span_last = datetime.strptime(max(sizes), HOUR_FMT)
        lines.append(
            _row(
                "archive",
                f"{total_gb * 1024:.0f} MB",
                f"{len(sizes)} hours, {span_first:%m-%d %H:00} .. {span_last:%m-%d %H:00} UTC",
            )
        )
        lines.append(
            _row(
                "burn rate",
                f"{per_day:.2f} GB/day",
                f"~{need:.1f} GB more to {DEADLINE:%d %b} ({days_left}d), {free:.0f} GB free",
            )
        )
        strip, good = coverage_strip(sizes, hours, age is not None and age <= STALE_SECONDS)
        lines.append(_row(f"last {hours}h", f"[{strip}]", f"{good}/{hours} hours"))
    else:
        lines.append(_row("archive", "EMPTY", str(data_dir)))
        healthy = False

    recent = notify.recent_alerts(7)
    if recent:
        lines.append(_row("alerts", f"{len(recent)} in 7 days", "newest last:"))
        for stamp, level, message in recent[-5:]:
            lines.append(f"                 {stamp[:16]}  {level:<5} {message[:70]}")
        if any(level in ("WARN", "CRIT") for _, level, _ in recent[-5:]):
            healthy = False
    else:
        lines.append(_row("alerts", "none in 7 days"))

    lines.append("")
    lines.append("  ALL GOOD" if healthy else "  NEEDS ATTENTION - see README, 'When it is not working'")
    return lines, healthy


def _tail_recorder_log(count: int = 25) -> list[str]:
    logs = sorted(config.STATE_DIR.glob("logs/recorder-*.log"))
    if not logs:
        return ["  (no recorder log - is it running under Task Scheduler?)"]
    newest = logs[-1]
    try:
        text = newest.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"  (cannot read {newest.name}: {exc})"]
    return [f"  --- {newest.name} ---"] + [f"  {ln}" for ln in text.splitlines()[-count:]]


def main() -> int:
    hours = 24
    if "--hours" in sys.argv:
        hours = max(6, min(int(sys.argv[sys.argv.index("--hours") + 1]), 168))

    lines, healthy = report(hours)
    print("\n".join(lines))

    if "--log" in sys.argv:
        print()
        print("\n".join(_tail_recorder_log()))

    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
