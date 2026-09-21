"""MarketGuard recorder.

Captures Binance top-20 order book snapshots (100ms) + aggregated trades for a
set of mid-cap pairs, writing hourly gzipped JSONL to data/.

Uses @depth20@100ms rather than @depth@100ms: every message is a complete
top-of-book picture, so there is no sequence tracking, no gap detection and no
book reconstruction. Spoofing happens near the top of book, so 20 levels is
sufficient for the primary detector.

Analysis uses Binance's server-side timestamps (the `E` field), not arrival
time, so network latency does not distort any downstream feature.
"""
import asyncio
import gzip
import shutil
import signal
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import websockets

# Mid-caps: active enough to have real order book behaviour, thin enough to be
# worth manipulating. BTC/ETH deliberately excluded - too deep to move.
SYMBOLS = [
    "solusdt",
    "avaxusdt",
    "injusdt",
    "seiusdt",
    "arbusdt",
    "opusdt",
]

OUT_DIR = Path(__file__).parent / "data"
WS_BASE = "wss://stream.binance.com:9443/stream?streams="

# Stop writing before the disk fills. A full disk kills the recording silently;
# this leaves room to notice and fix it.
MIN_FREE_GB = 5.0

# How often to log a per-stream breakdown. Used to decide whether six symbols
# is the right number - see README.
STATS_INTERVAL_S = 300

# Flush the gzip buffer at least this often. Bounds how much data a crash can
# lose; costs a little compression ratio at each block boundary.
FLUSH_INTERVAL_S = 15

_running = True


def _log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def _hour_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H")


def _free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1024**3


def _next_path(hour: str) -> Path:
    """Never append to an existing file.

    A process killed mid-write leaves a truncated gzip member. Appending a
    fresh member after it corrupts the whole file - and under Restart=always
    that is the normal path, not an edge case. So each open gets its own file.
    """
    candidate = OUT_DIR / f"binance_{hour}.jsonl.gz"
    seq = 1
    while candidate.exists():
        candidate = OUT_DIR / f"binance_{hour}.{seq}.jsonl.gz"
        seq += 1
    return candidate


def _stream_of(raw: str) -> str:
    """Pull the stream name out of {"stream":"solusdt@depth20@100ms",...

    Cheaper than a full json.loads on every message, and we write the raw
    line to disk anyway so we never need the parsed form here.
    """
    try:
        return raw.split('"', 4)[3]
    except IndexError:
        return "unknown"


async def record() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    streams = "/".join(f"{s}@depth20@100ms/{s}@aggTrade" for s in SYMBOLS)
    url = WS_BASE + streams

    fh = None
    current_hour = None
    hour_count = 0
    stats: Counter[str] = Counter()
    last_stats = time.monotonic()
    last_flush = last_stats

    _log(f"starting - {len(SYMBOLS)} symbols, {len(SYMBOLS) * 2} streams")
    _log(f"writing to {OUT_DIR}  ({_free_gb(OUT_DIR):.1f} GB free)")

    while _running:
        try:
            async with websockets.connect(url, ping_interval=20, max_size=None) as ws:
                _log("connected")

                async for raw in ws:
                    if not _running:
                        break
                    if isinstance(raw, bytes):
                        raw = raw.decode()

                    hour = _hour_key()
                    if hour != current_hour:
                        if fh:
                            fh.close()
                            _log(f"closed {current_hour} - {hour_count:,} messages")

                        free = _free_gb(OUT_DIR)
                        if free < MIN_FREE_GB:
                            _log(f"ABORT: {free:.1f} GB free, need {MIN_FREE_GB}")
                            return

                        path = _next_path(hour)
                        fh = gzip.open(path, "wt", encoding="utf-8")
                        current_hour = hour
                        hour_count = 0
                        _log(f"opened {path.name}  ({free:.1f} GB free)")

                    fh.write(raw)
                    fh.write("\n")
                    hour_count += 1
                    stats[_stream_of(raw)] += 1

                    now = time.monotonic()
                    if now - last_flush >= FLUSH_INTERVAL_S:
                        fh.flush()
                        last_flush = now

                    if now - last_stats >= STATS_INTERVAL_S:
                        total = sum(stats.values())
                        rate = total / (now - last_stats)
                        top = ", ".join(
                            f"{k.split('@')[0]}:{v:,}" for k, v in stats.most_common()
                        )
                        _log(f"{total:,} msgs @ {rate:.0f}/s | {top}")
                        stats.clear()
                        last_stats = now

        except asyncio.CancelledError:
            break
        except Exception as exc:
            _log(f"disconnected ({type(exc).__name__}: {exc}) - retrying in 5s")
            await asyncio.sleep(5)

    if fh:
        fh.close()
    _log("stopped cleanly")


def _stop(*_) -> None:
    global _running
    _running = False
    _log("shutdown requested")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except (AttributeError, ValueError):
        pass  # SIGTERM is not available on all platforms

    try:
        asyncio.run(record())
    except KeyboardInterrupt:
        pass
