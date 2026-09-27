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
constantly. --deep decompresses everything: it counts messages per symbol and
it scans message timestamps for sub-hour holes, which is authoritative but
slow.

The size test cannot see a hole inside an hour, and that is structural, not a
tuning error. An hour missing 23 of its 60 minutes still holds ~60% of its
usual bytes - nowhere near THIN_FRACTION, and tightening THIN_FRACTION to
catch it would flag every quiet Asian-session hour as a failure instead. The
threshold is right; the granularity is the problem. So hour classification
stays exactly as it was and gap detection is a second, finer pass over
timestamps rather than bytes - see deep_scan(), which measures per symbol,
and replay_windows(), which turns the result into windows.

That pass is the one that decides what may be offered for replay. A window is
not offered because its hours looked big enough; it is offered because every
symbol's message stream was verified unbroken across it - verify_window() is
the check, and it is meant to be asked *before* a window is used, not after
someone notices a flat panel on stage.

It also found a third failure mode that neither test was looking for: hour
files of normal size whose gzip stream will not decompress, which read as
good on bytes and as empty on content. See decoded_but_empty().

    python coverage.py
    python coverage.py --deep
    python coverage.py --deep --gap-threshold 30
    python coverage.py --deep --hours 20260922T14..20260927T02
"""
import collections
import dataclasses
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, NamedTuple

import config
import datafile

# An hour is "thin" below this fraction of the median hour's compressed size.
# Deliberately loose: real markets vary several-fold between the Asian lull and
# the US open, so a tighter threshold would flag genuinely quiet hours as
# failures. We are hunting for order-of-magnitude drops - the 11 KB hours that
# mean the lid was shut - not for below-average activity.
THIN_FRACTION = 0.25

# The depth stream is the heartbeat used for gap detection.
#
# It is pushed on a fixed 100ms cadence whether or not anything trades, so its
# absence is evidence about the *capture*, not about the market. Trades are
# the opposite: several of these pairs go minutes without one, so a silent
# aggTrade stream proves nothing and would produce a gap report made mostly of
# quiet symbols. Gaps are therefore measured on depth alone, and a symbol
# whose depth is flowing counts as covered.
#
# Depth snapshots carry no exchange timestamp, so datafile.timestamp() falls
# back to the recorder's receive stamp - which is the only clock they have.
# Some early files have that stamp interpolated rather than measured
# (datafile.is_estimated); the error is ~22ms median, five hundred times
# smaller than the smallest gap we are looking for, so it is ignored here.
HEARTBEAT_STREAM = "@depth"

# A hole at least this long is a gap. At the observed ~100ms median cadence
# that is ~600 missing snapshots per symbol.
#
# It has to clear normal jitter by a wide margin, because a false negative in
# a gap detector is the dangerous direction: a tool that invents a gap gets
# investigated, a tool that hides one gets trusted. Measured across the live
# archive, the widest interval that is not a gap is 9.4s - a websocket
# reconnect - so 60s sits about six times above the observed noise floor and
# cannot fire on healthy capture. It is also far shorter than any window
# anyone would actually replay, so no demo-length window can hide one
# underneath it.
#
# Six times is comfortable but it is not enormous, and it is the number to
# watch. Lowering the threshold would buy finer detection at the cost of
# firing on reconnects; that trade is worth making only if the reconnects
# stop, which is a recorder question rather than a coverage one.
#
# The report prints the widest interval that stayed *under* the threshold, so
# that headroom is measured on every run rather than asserted once here. If
# that number ever climbs toward the threshold, lower the threshold.
GAP_THRESHOLD_S = 60.0

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


# --------------------------------------------------------------------------
# Sub-hour gap detection
#
# Everything below reads timestamps rather than bytes, so it only runs on the
# --deep path. The cheap path's docstring promises it costs nothing, and this
# costs a full decompression of the archive.
# --------------------------------------------------------------------------

class Gap(NamedTuple):
    """A stretch during which one symbol produced no depth snapshot.

    start_ms is the last message before the hole and end_ms the first message
    after it, so the gap is bounded by two messages that really exist. It is
    therefore exactly the silence that was observed, not an inference about
    when the stream "should" have produced something - which matters because
    the expected cadence is a nominal 100ms that real traffic wanders around,
    and anything derived from it would be an estimate wearing a measurement's
    clothes.
    """
    symbol: str
    start_ms: int
    end_ms: int

    @property
    def seconds(self) -> float:
        return (self.end_ms - self.start_ms) / 1000.0


class Window(NamedTuple):
    """A stretch with no gap in it. What replay is allowed to be offered."""
    start_ms: int
    end_ms: int

    @property
    def seconds(self) -> float:
        return (self.end_ms - self.start_ms) / 1000.0


class Outage(NamedTuple):
    """Overlapping gaps across one or more symbols, reported as one event.

    The symbol set is the whole point. Six symbols dark at the same instant is
    a capture outage - the laptop slept, the process died, the wifi dropped.
    One symbol dark while the other five keep flowing is a stream problem -
    Binance stopped sending that pair, or that subscription was dropped on a
    reconnect. Same shape in the data, completely different causes, and
    flattening them into "a gap" throws away the distinction that tells you
    which one you are looking at.
    """
    start_ms: int
    end_ms: int
    symbols: tuple[str, ...]

    @property
    def seconds(self) -> float:
        return (self.end_ms - self.start_ms) / 1000.0

    def kind(self, total_symbols: int) -> str:
        return "capture outage" if len(self.symbols) >= total_symbols else "stream gap"


@dataclasses.dataclass
class Timeline:
    """Running coverage state for one symbol, fed one hour at a time.

    Holding every timestamp in the archive would be tens of millions of ints
    per symbol, so hours are folded in as they are read and only the running
    edge is kept.
    """
    symbol: str
    first_ms: int | None = None
    last_ms: int | None = None
    beats: int = 0
    gaps: list[Gap] = dataclasses.field(default_factory=list)
    widest_ok_ms: int = 0  # largest interval that stayed under the threshold

    def extend(self, timestamps: Iterable[int], threshold_ms: int) -> None:
        """Fold in one hour's sorted timestamps for this symbol.

        `timestamps` must be sorted, and the hours must arrive in order.
        Neither is true of raw file order: a restart file (...T15.1) holds
        data that continues ...T15, and datafile._sort_key is what puts those
        in sequence. Sorting within the hour and taking the running maximum
        across hours makes the result independent of how the hour's coverage
        happens to be split across files.

        A timestamp at or before the running edge is already covered, so it is
        skipped rather than treated as time going backwards. That is what
        makes an overlapping duplicate file harmless here: the same instants
        arriving twice cannot manufacture a gap.
        """
        for ts in timestamps:
            if self.last_ms is None:
                self.first_ms = self.last_ms = ts
                self.beats = 1
                continue
            if ts <= self.last_ms:
                continue
            delta = ts - self.last_ms
            if delta >= threshold_ms:
                self.gaps.append(Gap(self.symbol, self.last_ms, ts))
            elif delta > self.widest_ok_ms:
                self.widest_ok_ms = delta
            self.last_ms = ts
            self.beats += 1

    @property
    def dark_seconds(self) -> float:
        return sum(g.seconds for g in self.gaps)

    def windows(self) -> list[Window]:
        """The complement of the gaps: every stretch this symbol truly covers."""
        if self.first_ms is None or self.last_ms is None:
            return []
        out, start = [], self.first_ms
        for gap in self.gaps:
            if gap.start_ms > start:
                out.append(Window(start, gap.start_ms))
            start = gap.end_ms
        if self.last_ms > start:
            out.append(Window(start, self.last_ms))
        return out


def _scan_hour(paths: list[Path]) -> tuple[collections.Counter, dict[str, list[int]]]:
    """One hour's files -> message counts per symbol, heartbeat times per symbol.

    Deduplication is exactly datafile.read_all's: scoped to the hour, keyed on
    message identity, applied only when the hour has more than one file. Two
    files covering one hour are either a continuation after a restart or a
    genuine duplicate, and nothing in the filename says which - so the counts
    have to be taken after dedup or a duplicate hour reads as twice the
    traffic.

    For the timestamps the sort at the end is what actually matters. A
    repeated instant cannot invent a gap (Timeline.extend treats anything at
    or behind the running edge as already covered), but a continuation file
    read *before* the file it continues would present the hour's second half
    first, and that reads as a hole the width of the hour. datafile._sort_key
    orders the files; sorting the merged timestamps makes the result
    independent of how the hour's coverage happened to be split across them.
    """
    counts: collections.Counter[str] = collections.Counter()
    beats: dict[str, list[int]] = collections.defaultdict(list)
    dedup = len(paths) > 1
    seen: set[tuple] = set()

    for path in paths:
        for msg in datafile.read_messages(path):
            if dedup:
                key = datafile.message_id(msg)
                if key in seen:
                    continue
                seen.add(key)
            stream = msg.get("stream", "?")
            counts[stream.split("@")[0]] += 1
            if HEARTBEAT_STREAM in stream:
                ts = datafile.timestamp(msg)
                if ts is not None:
                    beats[stream.split("@")[0]].append(ts)

    for series in beats.values():
        series.sort()
    return counts, beats


class DeepScan(NamedTuple):
    """The result of one decompressing pass over the archive.

    `hours` is what was actually read, not what exists. Anything outside it is
    unknown, and a report that renders unknown as "clean" is the exact failure
    this module is here to stop - see _report_deep().
    """
    counts: dict[str, collections.Counter]   # hour -> symbol -> messages
    timelines: dict[str, Timeline]           # symbol -> coverage
    threshold_ms: int
    hours: tuple[str, ...] = ()

    @property
    def symbols(self) -> list[str]:
        return sorted(self.timelines)

    def gaps(self) -> list[Gap]:
        out = [g for tl in self.timelines.values() for g in tl.gaps]
        return sorted(out, key=lambda g: (g.start_ms, g.symbol))

    def outages(self) -> list[Outage]:
        return group_outages(self.gaps())

    def windows(self) -> dict[str, list[Window]]:
        return {sym: tl.windows() for sym, tl in self.timelines.items()}

    def replay_windows(self) -> list[Window]:
        """Windows contiguous for *every* symbol, longest first."""
        return replay_windows(self.windows())


def deep_scan(
    data_dir: Path | None = None,
    *,
    threshold_s: float = GAP_THRESHOLD_S,
    hours: Iterable[str] | None = None,
    progress: bool = False,
) -> DeepScan:
    """Decompress the archive once; count messages and find gaps in one pass.

    `hours` restricts the scan, and must name a *contiguous* range of hour
    keys. Skipping an hour in the middle would leave a hole that this cannot
    distinguish from a real one - which is the correct behaviour when the hour
    is genuinely absent from disk, and a lie if you merely declined to read
    it.

    The caller is expected to leave out the in-progress hour, for the reason
    given in split_in_progress(): its file has no end yet, so any window
    reaching into it is a window whose length changes while you look at it.
    """
    groups = datafile.files_by_hour(data_dir)
    if hours is not None:
        wanted = set(hours)
        groups = {h: p for h, p in groups.items() if h in wanted}

    threshold_ms = int(threshold_s * 1000)
    counts: dict[str, collections.Counter] = {}
    timelines: dict[str, Timeline] = {}

    ordered = sorted(groups)
    for i, hour in enumerate(ordered, 1):
        hour_counts, beats = _scan_hour(groups[hour])
        counts[hour] = hour_counts
        for symbol in sorted(beats):
            timelines.setdefault(symbol, Timeline(symbol)).extend(
                beats[symbol], threshold_ms
            )
        if progress:
            print(f"  {i}/{len(ordered)} {hour}  "
                  f"{sum(hour_counts.values()):>8,} msgs", flush=True)

    return DeepScan(counts, timelines, threshold_ms, tuple(ordered))


def group_outages(gaps: list[Gap]) -> list[Outage]:
    """Cluster overlapping per-symbol gaps into single events, longest first.

    Overlap, not equality: the six symbols do not go dark on the same
    millisecond, they go dark within a few hundred milliseconds of each other,
    and six near-identical lines in a report is noise that hides how many
    distinct events there actually were.
    """
    if not gaps:
        return []

    out: list[Outage] = []
    ordered = sorted(gaps, key=lambda g: (g.start_ms, g.end_ms))
    start, end = ordered[0].start_ms, ordered[0].end_ms
    members = {ordered[0].symbol}

    for gap in ordered[1:]:
        if gap.start_ms < end:
            end = max(end, gap.end_ms)
            members.add(gap.symbol)
        else:
            out.append(Outage(start, end, tuple(sorted(members))))
            start, end, members = gap.start_ms, gap.end_ms, {gap.symbol}
    out.append(Outage(start, end, tuple(sorted(members))))

    return sorted(out, key=lambda o: o.end_ms - o.start_ms, reverse=True)


def _intersect(a: list[Window], b: list[Window]) -> list[Window]:
    out: list[Window] = []
    i = j = 0
    while i < len(a) and j < len(b):
        start = max(a[i].start_ms, b[j].start_ms)
        end = min(a[i].end_ms, b[j].end_ms)
        if end > start:
            out.append(Window(start, end))
        if a[i].end_ms < b[j].end_ms:
            i += 1
        else:
            j += 1
    return out


def replay_windows(per_symbol: dict[str, list[Window]]) -> list[Window]:
    """Stretches contiguous for every symbol at once, longest first.

    The intersection rather than the union, because the demo plays all six
    symbols side by side. A window in which one pair is dark is not a window
    that can be replayed - it is a window in which one of the six panels is
    blank, which on stage is indistinguishable from a broken detector.
    """
    if not per_symbol:
        return []
    common = None
    for windows in per_symbol.values():
        common = windows if common is None else _intersect(common, windows)
        if not common:
            return []
    return sorted(common or [], key=lambda w: w.end_ms - w.start_ms, reverse=True)


def hours_covering(start_ms: int, end_ms: int) -> list[str]:
    """Every hour key a millisecond range touches, inclusive."""
    start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
    cur = start.replace(minute=0, second=0, microsecond=0)
    out = []
    while cur <= end:
        out.append(cur.strftime(HOUR_FMT))
        cur += timedelta(hours=1)
    return out


NOTHING = "(no symbol found)"


def verify_window(
    start_ms: int,
    end_ms: int,
    data_dir: Path | None = None,
    threshold_s: float = GAP_THRESHOLD_S,
    symbols: Iterable[str] | None = None,
) -> dict[str, list[Gap]]:
    """Check a proposed replay window. Empty result means it is clean.

    This is the guard that is supposed to stand between longest_runs() and
    anything that actually gets demoed. Ask it before offering a window, not
    after someone notices a flat panel.

    Uncovered *edges* count as gaps too: a window that starts thirty minutes
    before a symbol's first message is thirty minutes of blank screen, even
    though there is no pair of messages to sit between.

    Pass `symbols` when you know which pairs the replay needs. Without it the
    check can only speak about symbols that appear in the window, and a symbol
    that is wholly absent from it has no timeline to inspect - which would
    make total absence read as cleanliness, the one answer this must never
    give. A window containing nothing at all is therefore reported as one
    window-long gap rather than as an empty result.
    """
    scan = deep_scan(
        data_dir, threshold_s=threshold_s, hours=hours_covering(start_ms, end_ms)
    )
    threshold_ms = int(threshold_s * 1000)
    expected = sorted(symbols) if symbols is not None else sorted(scan.timelines)
    out: dict[str, list[Gap]] = {}

    if not expected:
        return {NOTHING: [Gap(NOTHING, start_ms, end_ms)]}

    for symbol in expected:
        timeline = scan.timelines.get(symbol)
        if timeline is None or timeline.first_ms is None:
            out[symbol] = [Gap(symbol, start_ms, end_ms)]
            continue

        holes = []
        for gap in timeline.gaps:
            lo, hi = max(gap.start_ms, start_ms), min(gap.end_ms, end_ms)
            if hi - lo >= threshold_ms:
                holes.append(Gap(symbol, lo, hi))
        if timeline.first_ms - start_ms >= threshold_ms:
            holes.insert(0, Gap(symbol, start_ms, timeline.first_ms))
        if end_ms - timeline.last_ms >= threshold_ms:
            holes.append(Gap(symbol, timeline.last_ms, end_ms))
        if holes:
            out[symbol] = sorted(holes, key=lambda g: g.start_ms)

    return out


def decoded_but_empty(
    sizes: dict[str, int], scan: DeepScan, threshold_bytes: float
) -> list[tuple[str, int, int]]:
    """Hours whose file is a normal size but decodes to almost nothing.

    A third failure mode, found by running this against the live archive and
    not anticipated when it was written.

    datafile.read_lines deliberately tolerates a truncated tail, because the
    recorder writes gzip continuously and a process killed mid-write leaves a
    file with no end-of-stream marker - everything before the cut really is
    valid. But the same tolerance silently absorbs a file whose deflate
    stream is corrupt near the *start*. Six megabytes on disk, two hundred
    lines readable, no exception reaching the caller. The size test sees six
    megabytes and calls the hour good. Nothing downstream ever learns that
    the hour is empty.

    The gap scan catches it anyway - an hour with no messages is an hour-long
    hole, and that is how it appears in the gap list. But "60.0 min capture
    outage" reads as "the laptop slept", which is the wrong repair. The bytes
    were captured; it is the file that is broken. Naming the two apart is the
    difference between checking the lid and checking the disk.

    Flagged when the hour passes the size test and still yields under 1% of
    the median hour's messages - a margin wide enough that no quiet market
    reaches it.
    """
    per_hour = {h: sum(c.values()) for h, c in scan.counts.items()}
    if not per_hour:
        return []
    typical = statistics.median(per_hour.values())

    out = []
    for hour in sorted(per_hour):
        msgs = per_hour[hour]
        if sizes.get(hour, 0) >= threshold_bytes and msgs < typical * 0.01:
            out.append((hour, sizes.get(hour, 0), msgs))
    return out


def run_bounds(first_hour: str, last_hour: str) -> tuple[int, int]:
    """Millisecond bounds of a size-based run of hours, end exclusive."""
    start = _parse_hour(first_hour)
    end = _parse_hour(last_hour) + timedelta(hours=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def holes_in_run(
    first_hour: str, last_hour: str, gaps: list[Gap]
) -> list[Gap]:
    """The gaps that fall inside a run longest_runs() would have offered."""
    start, end = run_bounds(first_hour, last_hour)
    return [g for g in gaps if g.end_ms > start and g.start_ms < end]


def _fmt(hour: str) -> str:
    return _parse_hour(hour).strftime("%m-%d %H:00")


def _fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%m-%d %H:%M:%S")


def _fmt_dur(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    return f"{int(seconds // 3600)}h {int(seconds % 3600) // 60:02d}m"


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
        print("\n=== longest unbroken runs (hour granularity) ===")
        for first, last, length in runs:
            print(f"  {length:>3}h   {_fmt(first)} .. {_fmt(last)}")

    usable = runs[0][2] if runs else 0
    print(f"\nlongest contiguous window: {usable}h  (unverified)")
    if usable < 6:
        print("  Too short to guarantee a replay containing an event.")
    print("  Hour granularity cannot see a hole inside an hour. These are")
    print("  replay *candidates*; run --deep to verify one before using it.")

    if "--deep" in sys.argv:
        threshold_s = _float_arg("--gap-threshold", GAP_THRESHOLD_S)
        hours = sorted(h for h in sizes if h != in_progress)
        window = _hours_arg("--hours")
        if window:
            hours = [h for h in hours if window[0] <= h <= window[1]]
            if not hours:
                print(f"\nno completed hours in {window[0]}..{window[1]}")
                return 1

        print(f"\n=== deep scan: {len(hours)} completed hours, "
              f"gap threshold {threshold_s:g}s ===")
        scan = deep_scan(data_dir, threshold_s=threshold_s,
                         hours=hours, progress=True)
        _report_deep(scan, runs, settled, median * THIN_FRACTION)

    return 0


def _float_arg(flag: str, default: float) -> float:
    if flag in sys.argv:
        return float(sys.argv[sys.argv.index(flag) + 1])
    return default


def _hours_arg(flag: str) -> tuple[str, str] | None:
    if flag not in sys.argv:
        return None
    first, _, last = sys.argv[sys.argv.index(flag) + 1].partition("..")
    return first, (last or first)


def _report_deep(
    scan: DeepScan,
    runs: list[tuple[str, str, int]],
    sizes: dict[str, int],
    threshold_bytes: float,
) -> None:
    symbols = scan.symbols
    totals: collections.Counter[str] = collections.Counter()
    for counts in scan.counts.values():
        totals.update(counts)

    print("\n=== per-symbol message counts (deduplicated) ===")
    for sym in sorted(totals):
        present = sum(1 for c in scan.counts.values() if c.get(sym))
        print(f"  {sym:<12} {totals[sym]:>10,} msgs   "
              f"present in {present}/{len(scan.counts)} hours")

    # Per symbol, because a gap in one stream and a gap in all six are
    # different failures. "longest clean" is the per-symbol answer to the
    # question replay actually asks; the all-symbol intersection is below.
    print("\n=== per-symbol heartbeat coverage (depth stream) ===")
    print(f"  {'symbol':<12} {'snapshots':>10}  {'gaps':>5} {'dark':>10}  "
          f"{'widest ok':>9}  {'covered':>8}  longest clean")
    for sym in symbols:
        tl = scan.timelines[sym]
        span = (tl.last_ms - tl.first_ms) / 1000 if tl.first_ms is not None else 0
        pct = 100 * (span - tl.dark_seconds) / span if span else 0.0
        own = max((w.seconds for w in tl.windows()), default=0.0)
        print(f"  {sym:<12} {tl.beats:>10,}  {len(tl.gaps):>5} "
              f"{_fmt_dur(tl.dark_seconds):>10}  "
              f"{tl.widest_ok_ms / 1000:>8.1f}s  {pct:>7.2f}%  "
              f"{_fmt_dur(own)}")
    # The headroom line. If "widest ok" ever creeps up toward the threshold,
    # the threshold is no longer safely above normal jitter and this report
    # has started being able to miss things.
    worst_ok = max((t.widest_ok_ms for t in scan.timelines.values()), default=0)
    print(f"\n  widest interval below the threshold: {worst_ok / 1000:.1f}s "
          f"(threshold {scan.threshold_ms / 1000:g}s) - "
          f"{scan.threshold_ms / max(worst_ok, 1):.1f}x headroom")

    broken = decoded_but_empty(sizes, scan, threshold_bytes)
    if broken:
        # Ahead of the gap list, because each of these also appears there as
        # an hour-long outage and this is what it actually was.
        print("\n=== hours that pass on size but decode to nothing ===")
        for hour, nbytes, msgs in broken:
            print(f"  {_fmt(hour)}   {nbytes / 1e6:>6.2f} MB on disk, "
                  f"{msgs:>6,} messages readable")
        print("  Corrupt gzip, not a capture gap. These are the same hours")
        print("  listed below as hour-long outages.")

    outages = scan.outages()
    if not outages:
        print("\n=== capture gaps ===\n  none")
    else:
        dark = sum(o.seconds for o in outages)
        plural = "event" if len(outages) == 1 else "events"
        print(f"\n=== capture gaps: {len(outages)} {plural}, "
              f"{_fmt_dur(dark)} uncovered ===")
        for out in outages:
            kind = out.kind(len(symbols))
            who = ("all symbols" if len(out.symbols) >= len(symbols)
                   else ", ".join(out.symbols))
            print(f"  {_fmt_ms(out.start_ms)} -> {_fmt_ms(out.end_ms)}  "
                  f"{_fmt_dur(out.seconds):>9}   {kind:<14} {who}")

    gaps = scan.gaps()
    scanned = set(scan.hours)
    if runs:
        print("\n=== the hour-granularity runs, re-checked ===")
        for first, last, length in runs:
            holes = holes_in_run(first, last, gaps)
            if not set(_hour_range(first, last)) <= scanned:
                # Never say "clean" about hours that were not read. An
                # unscanned run is unknown, and rendering unknown as clean
                # would put the false negative back into the report layer
                # after all the work below it to keep one out.
                verdict = "not scanned"
            elif not holes:
                verdict = "clean"
            else:
                events = group_outages(holes)
                verdict = (f"{len(events)} hole(s), "
                           f"{_fmt_dur(sum(e.seconds for e in events))} dark")
            print(f"  {length:>3}h   {_fmt(first)} .. {_fmt(last)}   {verdict}")

    windows = scan.replay_windows()
    print("\n=== verified replay windows (contiguous for all "
          f"{len(symbols)} symbols) ===")
    if not windows:
        print("  none")
        return
    for win in windows[:5]:
        print(f"  {_fmt_dur(win.seconds):>9}   "
              f"{_fmt_ms(win.start_ms)} -> {_fmt_ms(win.end_ms)}")

    best = windows[0]
    print(f"\nlongest genuinely contiguous window: {_fmt_dur(best.seconds)}"
          f"   {_fmt_ms(best.start_ms)} -> {_fmt_ms(best.end_ms)}")
    if runs and set(_hour_range(runs[0][0], runs[0][1])) <= scanned:
        if best.seconds < runs[0][2] * 3600:
            print(f"  Hour granularity claimed {runs[0][2]}h. "
                  f"The difference is holes inside hours that looked good.")
    else:
        print("  Bounded by the hours scanned; this is the best window in "
              "what was read, not necessarily in the archive.")


if __name__ == "__main__":
    raise SystemExit(main())
