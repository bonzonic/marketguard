"""Reading recorded data files.

Two wrinkles this module handles:

1. The recorder writes gzip continuously, so a file being actively written has
   no end-of-stream marker and its final line is usually partial. Python's
   gzip raises EOFError on that, which is benign - everything up to the
   truncation point is valid.

2. An hour can be covered by more than one file (``binance_...T14.jsonl.gz``
   and ``binance_...T14.1.jsonl.gz``). That happens either because the
   recorder restarted mid-hour (the second file is a *continuation*) or
   because two recorders ran at once (it is a *duplicate*). You cannot tell
   which from the filename, so we deduplicate on message identity instead.
"""
import gzip
import json
import re
import zlib
from pathlib import Path
from typing import Iterator

# Errors that mean "this file ends early", not "this file is unusable".
# A process killed mid-write leaves a truncated gzip member; depending on
# where the cut lands you get EOFError or a zlib decode failure. Either way
# everything decoded before that point is valid.
_TRUNCATED = (EOFError, OSError, zlib.error)

_HOUR_RE = re.compile(r"binance_(\d{8}T\d{2})")


def read_lines(path: Path | str) -> Iterator[str]:
    """Yield complete lines, tolerating a truncated tail.

    A file still being written ends mid-gzip-block and usually mid-line. We
    stop cleanly at the truncation point rather than raising.
    """
    pending = None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                if pending is not None:
                    yield pending
                # Hold each line back until we have seen the next one, so a
                # partial final line is never emitted.
                pending = line.rstrip("\n") if line.endswith("\n") else None
                if pending is None:
                    return
    except _TRUNCATED:
        pass  # mid-write or killed mid-write; what we yielded is still valid

    if pending:
        yield pending


def read_messages(path: Path | str) -> Iterator[dict]:
    """Yield parsed Binance combined-stream messages from one file.

    A fragment of a truncated line can still be valid JSON on its own - the
    tail of a price, for instance, parses as a bare number. So we require a
    well-formed combined-stream object rather than trusting json.loads.
    """
    for line in read_lines(path):
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and "stream" in msg and "data" in msg:
            yield msg


def message_id(msg: dict) -> tuple:
    """Stable identity for a message, used to deduplicate overlapping files.

    Depth snapshots carry ``lastUpdateId``; aggregated trades carry ``a``, the
    aggregate trade id. Both are unique per stream.
    """
    stream = msg.get("stream", "")
    data = msg.get("data", {})
    return (stream, data.get("lastUpdateId") or data.get("a"))


def data_files(data_dir: Path | str = "data") -> list[Path]:
    """All recorded files, oldest first."""
    return sorted(Path(data_dir).glob("binance_*.jsonl.gz"))


def files_by_hour(data_dir: Path | str = "data") -> dict[str, list[Path]]:
    """Recorded files grouped by the hour they cover."""
    groups: dict[str, list[Path]] = {}
    for path in data_files(data_dir):
        match = _HOUR_RE.match(path.name)
        if match:
            groups.setdefault(match.group(1), []).append(path)
    return groups


def read_all(data_dir: Path | str = "data", symbol: str | None = None) -> Iterator[dict]:
    """Yield every recorded message, deduplicated, in chronological order.

    Deduplication is scoped per hour so the seen-set stays bounded - duplicate
    coverage only ever occurs between files covering the same hour.
    """
    groups = files_by_hour(data_dir)
    for hour in sorted(groups):
        paths = groups[hour]
        seen: set[tuple] = set()
        for path in paths:
            for msg in read_messages(path):
                if symbol and not msg.get("stream", "").startswith(symbol):
                    continue
                if len(paths) > 1:
                    key = message_id(msg)
                    if key in seen:
                        continue
                    seen.add(key)
                yield msg


if __name__ == "__main__":
    import collections
    import sys

    data_dir = sys.argv[1] if len(sys.argv) > 1 else "data"
    groups = files_by_hour(data_dir)
    if not groups:
        print(f"no data files in {data_dir}/")
        raise SystemExit(1)

    hours = sorted(groups)
    dupes = {h: p for h, p in groups.items() if len(p) > 1}

    print(f"hours:  {len(hours)}  ({hours[0]} .. {hours[-1]})")
    print(f"files:  {sum(len(p) for p in groups.values())}")
    if dupes:
        print(f"        {len(dupes)} hour(s) with overlapping files - deduplicated on read")

    counts: collections.Counter[str] = collections.Counter()
    first_depth = first_trade = None

    for msg in read_all(data_dir):
        counts[msg["stream"]] += 1
        if first_depth is None and "depth" in msg["stream"]:
            first_depth = msg
        if first_trade is None and "aggTrade" in msg["stream"]:
            first_trade = msg

    print(f"\ntotal messages (deduplicated): {sum(counts.values()):,}\n")
    for stream, n in counts.most_common():
        print(f"  {stream:<30} {n:>9,}")

    if first_trade:
        d = first_trade["data"]
        print(f"\ntrade sample: {d['s']} price={d['p']} qty={d['q']} "
              f"buyer_is_maker={d['m']} ts={d['E']}")
    if first_depth:
        d = first_depth["data"]
        print(f"depth sample: {len(d['bids'])} bids / {len(d['asks'])} asks  "
              f"best_bid={d['bids'][0]}  best_ask={d['asks'][0]}")
