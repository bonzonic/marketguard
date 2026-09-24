"""What is actually in the archive, and what is missing from it.

Recording on a laptop means capture stops whenever the machine sleeps, loses
wifi, or gets closed. The resulting files are not absent - they are *small*,
which is far more dangerous, because a thin hour and a quiet market look
identical once the data is in Parquet. A detector trained or tuned across a
sleeping-laptop gap will conclude that liquidity vanished.

So this reports three things:

    gaps      hours with no file at all
    thin      hours with a file far below the typical size
    runs      the longest stretches of consecutive good hours

The third is the one that matters for the demo. Replay needs a *contiguous*
window containing an event - a month of capture with a hole every night still
cannot produce a clean three-minute replay if the holes fall in the wrong
place.

Default mode reads file sizes only, so it costs nothing and can be run
constantly. --deep decompresses everything and counts messages per symbol,
which is authoritative but slow.

    python coverage.py
    python coverage.py --deep
"""
import collections
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import datafile

# An hour is "thin" below this fraction of the median hour's compressed size.
# Deliberately loose: real markets vary several-fold between the Asian lull and
# the US open, so a tighter threshold would flag genuinely quiet hours as
# failures. We are hunting for order-of-magnitude drops - the 11 KB hours that
# mean the lid was shut - not for below-average activity.
THIN_FRACTION = 0.25

HOUR_FMT = "%Y%m%dT%H"


def _parse_hour(key: str) -> datetime:
    return datetime.strptime(key, HOUR_FMT).replace(tzinfo=timezone.utc)


def _hour_range(first: str, last: str) -> list[str]:
    """Every hour key from first to last inclusive, including missing ones."""
    start, end = _parse_hour(first), _parse_hour(last)
    out, cur = [], start
    while cur <= end:
        out.append(cur.strftime(HOUR_FMT))
        cur += timedelta(hours=1)
    return out


def hour_sizes(data_dir: Path | None = None) -> dict[str, int]:
    """Total compressed bytes per hour, summed across restart files.

    Summed rather than maxed: a restart mid-hour splits one hour's capture
    across two files, and together they cover the hour. Overlapping duplicates
    inflate this, which is the safe direction - it can mark a bad hour good but
    never a good hour bad, and --deep resolves the ambiguity properly.
    """
    return {
        hour: sum(p.stat().st_size for p in paths)
        for hour, paths in datafile.files_by_hour(data_dir).items()
    }


def classify(sizes: dict[str, int]) -> tuple[list[str], list[str], list[str], float]:
    """Split the full hour range into good, thin and missing."""
    if not sizes:
        return [], [], [], 0.0

    keys = sorted(sizes)
    median = statistics.median(sizes.values())
    threshold = median * THIN_FRACTION

    good, thin, missing = [], [], []
    for hour in _hour_range(keys[0], keys[-1]):
        if hour not in sizes:
            missing.append(hour)
        elif sizes[hour] < threshold:
            thin.append(hour)
        else:
            good.append(hour)
    return good, thin, missing, median


def longest_runs(good: list[str], limit: int = 5) -> list[tuple[str, str, int]]:
    """Consecutive stretches of good hours, longest first.

    Returned as (first_hour, last_hour, length). These are the only windows
    that can produce a replay without stitching across a gap.
    """
    if not good:
        return []

    runs: list[tuple[str, str, int]] = []
    start = prev = good[0]
    length = 1

    for hour in good[1:]:
        if _parse_hour(hour) - _parse_hour(prev) == timedelta(hours=1):
            length += 1
        else:
            runs.append((start, prev, length))
            start, length = hour, 1
        prev = hour
    runs.append((start, prev, length))

    return sorted(runs, key=lambda r: r[2], reverse=True)[:limit]


def deep_counts(data_dir: Path | None = None) -> dict[str, collections.Counter]:
    """Deduplicated message count per symbol per hour. Slow but authoritative."""
    per_hour: dict[str, collections.Counter] = {}
    groups = datafile.files_by_hour(data_dir)

    for i, hour in enumerate(sorted(groups), 1):
        counts: collections.Counter[str] = collections.Counter()
        seen: set[tuple] = set()
        paths = groups[hour]
        for path in paths:
            for msg in datafile.read_messages(path):
                if len(paths) > 1:
                    key = datafile.message_id(msg)
                    if key in seen:
                        continue
                    seen.add(key)
                counts[msg.get("stream", "?").split("@")[0]] += 1
        per_hour[hour] = counts
        print(f"  {i}/{len(groups)} {hour}  {sum(counts.values()):>8,} msgs", flush=True)

    return per_hour


def _fmt(hour: str) -> str:
    return _parse_hour(hour).strftime("%m-%d %H:00")


def main() -> int:
    data_dir = config.DATA_DIR
    sizes = hour_sizes(data_dir)
    if not sizes:
        print(f"no data files in {data_dir}")
        return 1

    good, thin, missing, median = classify(sizes)
    total = len(good) + len(thin) + len(missing)

    print(f"data:   {data_dir}")
    print(f"span:   {_fmt(min(sizes))} .. {_fmt(max(sizes))}  ({total} hours elapsed)")
    print(f"median hour: {median / 1e6:.1f} MB compressed\n")

    print(f"  good     {len(good):>4}   {100 * len(good) / total:>5.1f}%")
    print(f"  thin     {len(thin):>4}   {100 * len(thin) / total:>5.1f}%   "
          f"(under {median * THIN_FRACTION / 1e6:.1f} MB)")
    print(f"  missing  {len(missing):>4}   {100 * len(missing) / total:>5.1f}%")

    if thin:
        print("\n=== thin hours ===")
        for hour in thin:
            print(f"  {_fmt(hour)}   {sizes[hour] / 1e6:>7.3f} MB")

    if missing:
        print("\n=== missing hours ===")
        for hour in missing:
            print(f"  {_fmt(hour)}")

    runs = longest_runs(good)
    if runs:
        print("\n=== longest unbroken runs (replay candidates) ===")
        for first, last, length in runs:
            print(f"  {length:>3}h   {_fmt(first)} .. {_fmt(last)}")

    usable = runs[0][2] if runs else 0
    print(f"\nlongest contiguous window: {usable}h")
    if usable < 6:
        print("  Too short to guarantee a replay containing an event.")

    if "--deep" in sys.argv:
        print("\n=== per-symbol message counts (deduplicated) ===")
        per_hour = deep_counts(data_dir)
        symbols = sorted({s for c in per_hour.values() for s in c})
        totals = collections.Counter()
        for counts in per_hour.values():
            totals.update(counts)
        print()
        for sym in symbols:
            hours_present = sum(1 for c in per_hour.values() if c.get(sym))
            print(f"  {sym:<12} {totals[sym]:>10,} msgs   "
                  f"present in {hours_present}/{len(per_hour)} hours")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
