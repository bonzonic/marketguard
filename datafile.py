"""Reading recorded data files.

The recorder writes gzip continuously, so a file being actively written has no
end-of-stream marker and its final line is usually partial. Python's gzip
raises EOFError on that, which is benign - everything up to the truncation
point is valid. These helpers tolerate it so the extractor can read the
current hour without waiting for it to close.
"""
import gzip
import json
from pathlib import Path
from typing import Iterator


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
    except (EOFError, OSError):
        pass  # file is mid-write; everything yielded so far is valid

    if pending:
        yield pending


def read_messages(path: Path | str) -> Iterator[dict]:
    """Yield parsed Binance combined-stream messages."""
    for line in read_lines(path):
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def data_files(data_dir: Path | str = "data") -> list[Path]:
    """All recorded files, oldest first."""
    return sorted(Path(data_dir).glob("binance_*.jsonl.gz"))


if __name__ == "__main__":
    import collections
    import sys

    files = data_files(sys.argv[1] if len(sys.argv) > 1 else "data")
    if not files:
        print("no data files found")
        raise SystemExit(1)

    target = files[-1]
    counts: collections.Counter[str] = collections.Counter()
    first_depth = first_trade = None

    for msg in read_messages(target):
        stream = msg.get("stream", "?")
        counts[stream] += 1
        if first_depth is None and "depth" in stream:
            first_depth = msg
        if first_trade is None and "aggTrade" in stream:
            first_trade = msg

    print(f"file:  {target.name}")
    print(f"lines: {sum(counts.values()):,}\n")
    for stream, n in counts.most_common():
        print(f"  {stream:<30} {n:>7,}")

    if first_trade:
        d = first_trade["data"]
        print(f"\ntrade: {d['s']} price={d['p']} qty={d['q']} maker={d['m']}")
    if first_depth:
        d = first_depth["data"]
        print(f"depth: {len(d['bids'])} bids / {len(d['asks'])} asks"
              f"  best_bid={d['bids'][0]}  best_ask={d['asks'][0]}")
