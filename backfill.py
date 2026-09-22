"""Backfill timestamps onto files recorded before receive-stamping existed.

Partial book depth streams carry no exchange timestamp, so early recordings
have no clock on the depth stream at all. That data cannot be re-captured, but
it can be reconstructed: depth messages are interleaved with aggTrade messages
that *do* carry an event time (``E``), and messages are written in arrival
order. So every unstamped message sits between two trades whose event times
bracket it, and can be interpolated by position.

Accuracy is roughly +/- 100ms - trades arrive every ~200ms across all streams,
and interpolation is linear by message index within each bracket. That is fine
for volume and pump-window analysis and marginal for spoof lifetimes, so
reconstructed values are flagged:

    {"t": 1790009715357, "est": 1, "stream": ..., "data": ...}

``est`` is absent on genuinely measured timestamps. Anything depending on
sub-100ms precision should skip messages carrying it.

Usage:
    python backfill.py [data_dir] [--dry-run]
"""
import gzip
import re
import sys
import tempfile
from pathlib import Path

from datafile import _TRUNCATED, data_files

_EVENT_TIME = re.compile(rb'"E":(\d{13})')
_ALREADY_STAMPED = re.compile(rb'^\{"t":')


def _read_raw(path: Path) -> list[bytes]:
    """All complete lines of a file, tolerating a truncated tail."""
    lines: list[bytes] = []
    pending: bytes | None = None
    try:
        with gzip.open(path, "rb") as fh:
            for line in fh:
                if pending is not None:
                    lines.append(pending)
                pending = line.rstrip(b"\n") if line.endswith(b"\n") else None
                if pending is None:
                    break
    except _TRUNCATED:
        pass
    if pending:
        lines.append(pending)
    return lines


def _interpolate(lines: list[bytes]) -> tuple[list[int | None], int, int]:
    """Assign a millisecond timestamp to every line.

    Lines carrying an exchange event time anchor the interpolation; everything
    between two anchors is spread linearly by index. Lines before the first
    anchor or after the last inherit that anchor's time.
    """
    anchors: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        match = _EVENT_TIME.search(line)
        if match:
            anchors.append((i, int(match.group(1))))

    if not anchors:
        return [None] * len(lines), 0, len(lines)

    stamps: list[int | None] = [None] * len(lines)
    exact = 0

    for i, t in anchors:
        stamps[i] = t
        exact += 1

    # Leading and trailing runs inherit the nearest anchor.
    for i in range(anchors[0][0]):
        stamps[i] = anchors[0][1]
    for i in range(anchors[-1][0] + 1, len(lines)):
        stamps[i] = anchors[-1][1]

    # Interior gaps spread linearly by index.
    for (i0, t0), (i1, t1) in zip(anchors, anchors[1:]):
        span = i1 - i0
        if span <= 1:
            continue
        for k in range(1, span):
            stamps[i0 + k] = t0 + round((t1 - t0) * k / span)

    return stamps, exact, len(lines) - exact


def _stamp(line: bytes, ts: int, estimated: bool) -> bytes:
    prefix = b'{"t":' + str(ts).encode()
    if estimated:
        prefix += b',"est":1'
    return prefix + b"," + line[1:]


def backfill(path: Path, dry_run: bool = False) -> tuple[int, int] | None:
    """Rewrite one file with timestamps. Returns (exact, estimated) or None."""
    lines = _read_raw(path)
    if not lines:
        return None
    if _ALREADY_STAMPED.match(lines[0]):
        return None

    stamps, exact, estimated = _interpolate(lines)
    if stamps[0] is None:
        print(f"  {path.name}: no trade anchors, cannot interpolate - skipped")
        return None

    if dry_run:
        return exact, estimated

    # Write to a temp file in the same directory and replace atomically, so an
    # interrupted run can never leave a half-written recording behind.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        import os

        os.close(fd)
        with gzip.open(tmp, "wb") as out:
            for line, ts in zip(lines, stamps):
                out.write(_stamp(line, ts, _EVENT_TIME.search(line) is None))
                out.write(b"\n")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    return exact, estimated


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    data_dir = Path(args[0]) if args else Path("data")

    files = data_files(data_dir)
    if not files:
        print(f"no data files in {data_dir}/")
        return 1

    if dry_run:
        print("DRY RUN - no files will be modified\n")

    done = total_exact = total_est = 0
    for path in files:
        result = backfill(path, dry_run)
        if result is None:
            continue
        exact, est = result
        done += 1
        total_exact += exact
        total_est += est
        share = est / (exact + est) * 100 if exact + est else 0
        print(f"  {path.name}: {exact:>7,} exact  {est:>7,} estimated ({share:.0f}%)")

    if not done:
        print("nothing to do - all files already stamped")
        return 0

    print(f"\n{done} file(s) {'would be ' if dry_run else ''}rewritten")
    print(f"  exact (exchange event time): {total_exact:,}")
    print(f"  estimated (interpolated):    {total_est:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
