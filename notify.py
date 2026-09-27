"""Being told the recording stopped, without having to remember to check.

A monitor nobody reads is not monitoring. The recorder can already detect
that it is out of disk and stop - and that is exactly the shape of failure
that goes unnoticed for a week, because the symptom is silence. So every
check that fails has to push a message somewhere a human will actually see
it.

Four channels, in the order they are worth having:

    event log   always attempted, needs nothing, and is what fires the
                desktop popup task (see ops/install-tasks.ps1). Local only:
                useless while the human is out of the house.
    popup       a message box in the logged-on session, triggered by the
                event log entry above. Zero configuration, zero accounts.
    ntfy        HTTP POST to ntfy.sh, which needs no account at all - pick an
                unguessable topic, install the app, and alerts reach a phone.
                The only channel that works when nobody is at the machine.
    webhook     generic JSON POST for a Slack or Discord incoming webhook,
                for anyone who already has one.

Everything is also appended to alerts.log, which is what `status.py` reads -
so an alert missed at 3am is still visible the next morning.

Configuration comes from the environment, which config.py fills in from
C:\\ProgramData\\MarketGuard\\marketguard.env:

    MARKETGUARD_NTFY_TOPIC      enables ntfy; treat it as a password
    MARKETGUARD_NTFY_SERVER     default https://ntfy.sh
    MARKETGUARD_WEBHOOK_URL     enables the webhook
    MARKETGUARD_WEBHOOK_FIELD   JSON key for the message; "text" suits Slack,
                                "content" suits Discord

No new dependencies - urllib is enough, and a monitoring path that can break
because a wheel failed to build is not worth having.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import config

# Only 1001 fires the desktop popup. The daily heartbeat uses 1000 so that
# proving the channel still works does not itself interrupt anyone.
EVENT_ID_ALERT = 1001
EVENT_ID_INFO = 1000
EVENT_SOURCE = "MarketGuard"

_ENTRY_TYPE = {"INFO": "Information", "WARN": "Warning", "CRIT": "Error"}
_NTFY_PRIORITY = {"INFO": "low", "WARN": "high", "CRIT": "urgent"}
_NTFY_TAGS = {"INFO": "chart_with_upwards_trend", "WARN": "warning", "CRIT": "rotating_light"}

# Short enough that a hung proxy cannot stall the watchdog past its next run.
_HTTP_TIMEOUT_S = 15


def _write_event_log(level: str, title: str, body: str) -> bool:
    """Best-effort Application log entry; also the popup task's trigger.

    Shelled out to PowerShell rather than done with pywin32 to avoid a
    dependency. The message goes through the environment instead of the
    command line because alert text contains quotes and newlines, and
    building a correctly escaped command line for those is a bug waiting to
    happen.
    """
    event_id = EVENT_ID_ALERT if level in ("WARN", "CRIT") else EVENT_ID_INFO
    env = dict(os.environ, MG_ALERT_MESSAGE=f"{title}\n\n{body}")
    script = (
        f"Write-EventLog -LogName Application -Source '{EVENT_SOURCE}' "
        f"-EventId {event_id} -EntryType {_ENTRY_TYPE[level]} "
        "-Message $env:MG_ALERT_MESSAGE"
    )
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=30,
            env=env,
        )
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _post(url: str, data: bytes, headers: dict[str, str]) -> bool:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _send_ntfy(level: str, title: str, body: str) -> bool:
    topic = os.environ.get("MARKETGUARD_NTFY_TOPIC")
    if not topic:
        return False
    server = os.environ.get("MARKETGUARD_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    return _post(
        f"{server}/{topic}",
        body.encode("utf-8"),
        {
            # ASCII-only: ntfy rejects non-Latin-1 header bytes.
            "Title": title.encode("ascii", "replace").decode("ascii"),
            "Priority": _NTFY_PRIORITY[level],
            "Tags": _NTFY_TAGS[level],
        },
    )


def _send_webhook(level: str, title: str, body: str) -> bool:
    url = os.environ.get("MARKETGUARD_WEBHOOK_URL")
    if not url:
        return False
    field = os.environ.get("MARKETGUARD_WEBHOOK_FIELD", "text")
    payload = json.dumps({field: f"[{level}] {title}\n{body}"}).encode("utf-8")
    return _post(url, payload, {"Content-Type": "application/json"})


def _append_trail(level: str, title: str, body: str) -> None:
    """One line per alert, tab separated, newest last.

    Flattened to a single line so the file stays greppable and so status.py
    can show the last few without parsing anything.
    """
    directory = config.STATE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    flat = " ".join(f"{title} - {body}".split())
    with open(directory / "alerts.log", "a", encoding="utf-8") as fh:
        fh.write(f"{stamp}\t{level}\t{flat}\n")


def alert(level: str, title: str, body: str) -> list[str]:
    """Push an alert everywhere configured. Returns the channels that took it.

    Never raises. A monitor that dies trying to report a problem has turned
    one failure into two, and the second one is invisible.
    """
    if level not in _ENTRY_TYPE:
        level = "WARN"

    delivered = []
    try:
        _append_trail(level, title, body)
        delivered.append("log")
    except OSError:
        pass
    if _write_event_log(level, title, body):
        delivered.append("eventlog")
    if _send_ntfy(level, title, body):
        delivered.append("ntfy")
    if _send_webhook(level, title, body):
        delivered.append("webhook")
    return delivered


def recent_alerts(days: int = 7) -> list[tuple[str, str, str]]:
    """(timestamp, level, message) for the last `days`, oldest first."""
    path = config.STATE_DIR / "alerts.log"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    cutoff = datetime.now().astimezone() - timedelta(days=days)
    out = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        try:
            when = datetime.fromisoformat(parts[0])
        except ValueError:
            continue
        if when >= cutoff:
            out.append((parts[0], parts[1], parts[2]))
    return out


def channels() -> list[str]:
    """Which push channels are configured, for install-time sanity checks."""
    out = ["eventlog+popup"]
    if os.environ.get("MARKETGUARD_NTFY_TOPIC"):
        out.append("ntfy")
    if os.environ.get("MARKETGUARD_WEBHOOK_URL"):
        out.append("webhook")
    return out


if __name__ == "__main__":
    level = sys.argv[1].upper() if len(sys.argv) > 1 else "WARN"
    took = alert(level, "MarketGuard test alert", "Sent by hand from notify.py.")
    print(f"configured: {', '.join(channels())}")
    print(f"delivered:  {', '.join(took) if took else 'NOTHING - alerts will not reach you'}")
    raise SystemExit(0 if len(took) > 1 else 1)
