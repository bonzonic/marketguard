"""Where recorded data lives.

The recorder appends continuously and flushes every 15 seconds, so the data
directory must not sit inside a synced folder. OneDrive re-uploads a file on
every change, which means constant churn, quota burn, and a real risk of the
sync client copying a gzip file mid-write.

So if the repository itself lives under OneDrive - which is a perfectly
reasonable place for code - the data defaults to somewhere outside it.
Override explicitly with the MARKETGUARD_DATA environment variable.
"""
import os
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent


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


def describe() -> str:
    source = "MARKETGUARD_DATA" if os.environ.get("MARKETGUARD_DATA") else "default"
    note = ""
    if not os.environ.get("MARKETGUARD_DATA") and _in_synced_folder(REPO_DIR):
        note = "  (repo is in a synced folder, so data is kept outside it)"
    return f"data: {DATA_DIR}  [{source}]{note}"


if __name__ == "__main__":
    print(describe())
    print(f"parquet: {PARQUET_DIR}")
