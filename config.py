"""Where recorded data lives, and where machine state lives.

The recorder appends continuously and flushes every 15 seconds, so the data
directory must not sit inside a synced folder. OneDrive re-uploads a file on
every change, which means constant churn, quota burn, and a real risk of the
sync client copying a gzip file mid-write.

So if the repository itself lives under OneDrive - which is a perfectly
reasonable place for code - the data defaults to somewhere outside it.
Override explicitly with the MARKETGUARD_DATA environment variable.

Running unattended adds a second problem: the scheduled tasks and a human at
a prompt have completely different environments, and if they disagree about
the data directory the status command reports confidently on an archive that
is not the one being written. So settings also come from a machine config
file, read here at import - before DATA_DIR is computed, which is the whole
point. A real environment variable always wins over the file, so an explicit
override in a shell still does what it looks like it does.
"""
import os
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent


def state_dir() -> Path:
    """Logs, alert history, watchdog state - machine state, not repository
    state. Kept out of the checkout so git status stays meaningful and so a
    task running as SYSTEM is not writing into a working tree."""
    override = os.environ.get("MARKETGUARD_STATE")
    if override:
        return Path(override)
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "MarketGuard"


def load_machine_config(path: Path | None = None) -> None:
    """Merge marketguard.env into the environment. Missing file is normal."""
    path = path or state_dir() / "marketguard.env"
    try:
        # utf-8-sig, not utf-8: PowerShell's Set-Content -Encoding utf8 writes
        # a BOM, and reading it as plain utf-8 turns the first key into
        # "﻿MARKETGUARD_DATA", which silently fails to match anything.
        # The symptom is a recorder writing to the wrong directory.
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


load_machine_config()


def _synced_roots() -> list[Path]:
    roots = []
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(var)
        if value:
            roots.append(Path(value).resolve())
    return roots


def _in_synced_folder(path: Path) -> bool:
    return any(path == root or root in path.parents for root in _synced_roots())


def default_data_dir() -> Path:
    if _in_synced_folder(REPO_DIR):
        return Path.home() / "marketguard-data"
    return REPO_DIR / "data"


def data_dir() -> Path:
    override = os.environ.get("MARKETGUARD_DATA")
    return Path(override).resolve() if override else default_data_dir()


DATA_DIR = data_dir()
PARQUET_DIR = DATA_DIR.parent / "parquet"
STATE_DIR = state_dir()


def describe() -> str:
    source = "MARKETGUARD_DATA" if os.environ.get("MARKETGUARD_DATA") else "default"
    note = ""
    if not os.environ.get("MARKETGUARD_DATA") and _in_synced_folder(REPO_DIR):
        note = "  (repo is in a synced folder, so data is kept outside it)"
    return f"data: {DATA_DIR}  [{source}]{note}"


if __name__ == "__main__":
    print(describe())
    print(f"parquet: {PARQUET_DIR}")
    print(f"state:   {STATE_DIR}")
