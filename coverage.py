"""What is actually in the archive, and what is missing from it.

Recording on a laptop means capture stops whenever the machine sleeps, loses
wifi, or gets closed. The resulting files are not absent - they are *small*,
which is far more dangerous, because a thin hour and a quiet market look
identical once the data is in Parquet. A detector trained or tuned across a
sleeping-laptop gap will conclude that liquidity vanished.

So this reports four things:

    gaps      hours with no file at all
    thin      hours with a file far below the typical size
    runs      the longest stretches of consecutive good hours
    current   the hour being recorded right now, reported but never judged

The third is the one that matters for the demo. Replay needs a *contiguous*
window containing an event - a month of capture with a hole every night still
cannot produce a clean three-minute replay if the holes fall in the wrong
place.

The fourth exists because the hour we are currently inside is not comparable
to the hours behind it - see split_in_progress().

Default mode reads file sizes only, so it costs nothing and can be run
constantly. --deep decompresses everything and counts messages per symbol,
which is authoritative but slow.

    python coverage.py
    python coverage.py --deep
"""
import collections
import os
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


def _file_size(path: Path) -> int:
    """Compressed bytes in one file, measured through an open handle.

    Not path.stat().st_size, because of how Windows reports the size of a file
    that is open for writing. NTFS keeps the authoritative size in the file's
    own metadata and pushes it back into the *parent directory's* entry
    lazily - not on every write. Anything that reads the directory entry can
    therefore see a stale size for the file the recorder is appending to right
    now, including zero for a file that already holds megabytes.

    os.stat() normally opens a handle and asks the file system driver, which
    is accurate - sampling the live file every five seconds for half an hour
    never once caught it disagreeing with the handle, so this is not the
    everyday path. But CPython falls back to a directory enumeration whenever
    it cannot get that handle, and a file being written is exactly the thing
    that produces transient open failures (indexers, AV, sync clients). That
    fallback is the only route by which the 0 bytes once reported for a file
    holding 6.8 MB could have been produced, and it is rare enough that it
    was not reproduced here.

    Which is the reason to close it off rather than argue about it. A fault
    that shows up on one run in a hundred is worse than a constant one, and
    seeking to the end of an open handle cannot consult the stale copy at
    all. It costs one open per file - nothing against a few hundred files,
    and this is not the slow path - and it makes the question moot. stat()
    stays as the fallback for a file that genuinely cannot be opened.
    """
    try:
        with open(path, "rb") as fh:
            return fh.seek(0, os.SEEK_END)
    except OSError:
        return path.stat().st_size


def hour_sizes(data_dir: Path | None = None) -> dict[str, int]:
    """Total compressed bytes per hour, summed across restart files.

    Summed rather than maxed: a restart mid-hour splits one hour's capture
    across two files, and together they cover the hour. Overlapping duplicates
    inflate this, which is the safe direction - it can mark a bad hour good but
    never a good hour bad, and --deep resolves the ambiguity properly.
    """
    return {
        hour: sum(_file_size(p) for p in paths)
        for hour, paths in datafile.files_by_hour(data_dir).items()
    }


def split_in_progress(
    sizes: dict[str, int], now: datetime | None = None
) -> tuple[dict[str, int], str | None]:
    """Separate the hour still being recorded from the hours that are done.

    The hour we are currently inside is not a small hour - it is an unfinished
    one, and the difference matters enormously. Five minutes past the hour the
    recorder has written maybe 0.6 MB of an eventual 7 MB, and within the
    first flush interval the file on disk is literally empty, because the
    recorder has opened it but gzip has not pushed a block out yet. Compare
    any of that against the median of *completed* hours and it is "thin" by a
    factor of ten.

    That is not a detection. It is an arithmetic certainty that fires every
    hour, for the first ten minutes or so of every hour, on an archive that is
    in perfect health. And because whether you see it depends on what minute
    you happen to run the tool, it looks like an intermittent fault in the
    recorder rather than a constant in the report - which is worse than a
    steady error, because a steady error gets disbelieved and this one gets
    investigated.

    Three ways out were available. Measuring the partial hour more accurately
    does not help: the number was never the problem, the comparison was, and a
    perfectly measured 0.6 MB is still going to lose to a 1.8 MB threshold.
    Dropping the hour silently would work, but it throws away the one line in
    this report that answers "is the recorder alive right now", which is worth
    keeping. So the hour is pulled out and reported on its own: visible, with
    its real size, and structurally unable to contaminate the thin/missing
    counts or the median that defines the threshold.

    It is excluded from longest_runs() for the same reason but a sharper one.
    Those runs exist to name windows that can be replayed, and a window whose
    last hour is a file still being appended to cannot be replayed - it has no
    end yet, its final gzip block is incomplete, and it will be a different
    length by the time anyone acts on the answer. Counting it inflates every
    "longest contiguous window" by one hour and puts the boundary of a demo
    replay inside a file that is still moving.

    Defined by the wall clock, not by "the newest hour present". If the
    recorder died three hours ago, the newest hour on disk is over and should
    be judged like any other - a stopped recorder is precisely what this tool
    exists to show. Only the hour we are literally inside gets the exemption,
    and only if a file for it exists at all.
    """
    now = now or datetime.now(timezone.utc)
    hour = now.strftime(HOUR_FMT)
    if hour not in sizes:
        return dict(sizes), None
    return {h: n for h, n in sizes.items() if h != hour}, hour


def classify(sizes: dict[str, int]) -> tuple[list[str], list[str], list[str], float]:
    """Split the full hour range into good, thin and missing.

    Expects completed hours only. Pass it the first element of
    split_in_progress(), never the raw hour_sizes() - an unfinished hour in
    here drags the median down and lands in `thin` on its own account.
    """
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

    settled, in_progress = split_in_progress(sizes)
    good, thin, missing, median = classify(settled)
    total = len(good) + len(thin) + len(missing)

    elapsed = f"{total} complete hours"
    if in_progress:
        elapsed += " + 1 in progress"

    print(f"data:   {data_dir}")
    print(f"span:   {_fmt(min(sizes))} .. {_fmt(max(sizes))}  ({elapsed})")
    print(f"median hour: {median / 1e6:.1f} MB compressed\n")

    if not total:
        # Nothing but the hour we are inside. Everything below divides by the
        # number of completed hours, and there is genuinely nothing to judge.
        print("  no completed hours yet")
        if in_progress:
            print(f"\nin progress: {_fmt(in_progress)}   "
                  f"{sizes[in_progress] / 1e6:.3f} MB so far")
        return 0

    print(f"  good     {len(good):>4}   {100 * len(good) / total:>5.1f}%")
    print(f"  thin     {len(thin):>4}   {100 * len(thin) / total:>5.1f}%   "
          f"(under {median * THIN_FRACTION / 1e6:.1f} MB)")
    print(f"  missing  {len(missing):>4}   {100 * len(missing) / total:>5.1f}%")

    if in_progress:
        # Reported, never classified. Its size is a fraction of a finished
        # hour's by construction, so calling it thin or good says nothing
        # about the archive - only about what minute it is.
        print(f"  current  {sizes[in_progress] / 1e6:>7.3f} MB   "
              f"{_fmt(in_progress)}, still recording")

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
