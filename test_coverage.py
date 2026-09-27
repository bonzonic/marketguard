"""Tests for the in-progress hour, which is the only part of coverage.py that
can be wrong about a healthy archive.

Everything here builds its own files in a temp directory. Nothing touches the
real recording - the archive is not reproducible and a test is not a good
enough reason to have a process reading it in a loop.

    python -m pytest test_coverage.py -q
"""
import collections
import gzip
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import coverage
import datafile

HOUR_FMT = coverage.HOUR_FMT

# A realistic complete hour, from the live archive: ~7 MB compressed.
FULL = 7_000_000


def _key(base: datetime, offset: int) -> str:
    return (base + timedelta(hours=offset)).strftime(HOUR_FMT)


def _write(tmp_path, hour: str, nbytes: int, seq: int | None = None):
    """A file of a given size under the recorder's naming scheme."""
    suffix = "" if seq is None else f".{seq}"
    path = tmp_path / f"binance_{hour}{suffix}.jsonl.gz"
    path.write_bytes(b"\0" * nbytes)
    return path


# --------------------------------------------------------------------------
# split_in_progress
# --------------------------------------------------------------------------

def test_current_hour_is_separated_from_the_completed_ones():
    now = datetime(2026, 9, 24, 16, 41, tzinfo=timezone.utc)
    sizes = {_key(now, -2): FULL, _key(now, -1): FULL, _key(now, 0): 600_000}

    settled, in_progress = coverage.split_in_progress(sizes, now=now)

    assert in_progress == "20260924T16"
    assert "20260924T16" not in settled
    assert len(settled) == 2


def test_no_file_for_the_current_hour_means_nothing_is_in_progress():
    """A gap that reaches the present is a gap, not an hour in progress."""
    now = datetime(2026, 9, 24, 16, 41, tzinfo=timezone.utc)
    sizes = {_key(now, -3): FULL, _key(now, -2): FULL}

    settled, in_progress = coverage.split_in_progress(sizes, now=now)

    assert in_progress is None
    assert settled == sizes


def test_a_stale_newest_hour_is_still_judged():
    """The exemption is for the hour we are inside, not for the newest file.

    If the recorder died three hours ago its last hour is finished - probably
    badly - and hiding it would suppress exactly the failure this tool exists
    to surface.
    """
    now = datetime(2026, 9, 24, 16, 41, tzinfo=timezone.utc)
    sizes = {_key(now, -5): FULL, _key(now, -4): FULL, _key(now, -3): 11_000}

    settled, in_progress = coverage.split_in_progress(sizes, now=now)

    assert in_progress is None
    assert settled == sizes
    _, thin, _, _ = coverage.classify(settled)
    assert thin == [_key(now, -3)]


def test_split_does_not_mutate_its_input():
    now = datetime(2026, 9, 24, 16, 41, tzinfo=timezone.utc)
    sizes = {_key(now, -1): FULL, _key(now, 0): 600_000}
    before = dict(sizes)

    coverage.split_in_progress(sizes, now=now)

    assert sizes == before


# --------------------------------------------------------------------------
# The regression itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "partial, minutes",
    [
        (0, 0),          # recorder just opened the file; gzip has not flushed
        (35_000, 1),     # one minute in
        (600_000, 5),    # five minutes in
        (1_200_000, 10), # ten minutes in, still under the 1.75 MB threshold
    ],
)
def test_a_partial_current_hour_is_never_called_thin(partial, minutes):
    """The bug: an unfinished hour judged against completed hours.

    Every one of these sizes is correct - that is the point. The hour really
    does contain only that many bytes so far, and it is still perfectly
    healthy. Only the comparison was wrong.
    """
    now = datetime(2026, 9, 24, 16, minutes, tzinfo=timezone.utc)
    sizes = {_key(now, -n): FULL for n in range(1, 13)}
    sizes[_key(now, 0)] = partial

    # What the old code did: classify everything together.
    _, thin_before, _, _ = coverage.classify(sizes)
    assert _key(now, 0) in thin_before, "expected the old behaviour to flag it"

    # What it does now.
    settled, in_progress = coverage.split_in_progress(sizes, now=now)
    good, thin, missing, _ = coverage.classify(settled)

    assert in_progress == _key(now, 0)
    assert thin == []
    assert missing == []
    assert len(good) == 12


def test_the_current_hour_cannot_appear_in_a_replay_window():
    """Runs name windows that can be replayed. A file still being appended to
    has no end, so a run must stop at the last completed hour.
    """
    now = datetime(2026, 9, 24, 16, 41, tzinfo=timezone.utc)
    sizes = {_key(now, -n): FULL for n in range(1, 13)}
    sizes[_key(now, 0)] = 5_000_000  # big enough to pass as "good" on size

    good_before, _, _, _ = coverage.classify(sizes)
    assert coverage.longest_runs(good_before)[0] == (
        _key(now, -12), _key(now, 0), 13
    ), "expected the old behaviour to include the in-progress hour"

    settled, _ = coverage.split_in_progress(sizes, now=now)
    good, _, _, _ = coverage.classify(settled)
    first, last, length = coverage.longest_runs(good)[0]

    assert length == 12
    assert last == _key(now, -1)
    assert first == _key(now, -12)


def test_the_current_hour_does_not_drag_the_median_down():
    """The second-order damage, which is quieter and worse.

    An unfinished hour left in the sample pulls the median down, and the
    median sets the threshold every *other* hour is held to. So a partial
    current hour does not only mislabel itself - it raises the bar for what
    counts as thin and can let a genuinely truncated hour through.

    Sizes here are real consecutive hours from the archive, because the effect
    needs the spread that real hours have.
    """
    now = datetime(2026, 9, 24, 16, 5, tzinfo=timezone.utc)
    real = [5_675_377, 6_135_389, 6_249_304, 7_130_666,
            7_895_131, 9_686_791, 10_865_568]
    sizes = {_key(now, -n): size for n, size in enumerate(reversed(real), 1)}
    sizes[_key(now, 0)] = 200_000

    _, _, _, median_before = coverage.classify(sizes)
    settled, _ = coverage.split_in_progress(sizes, now=now)
    _, _, _, median_after = coverage.classify(settled)

    assert median_after == 7_130_666
    assert median_before < median_after

    # A genuinely truncated hour that the depressed threshold would have let
    # through. 1.70 MB is under 25% of the true median but over 25% of the
    # median the in-progress hour produces.
    truncated = 1_700_000
    assert median_before * coverage.THIN_FRACTION < truncated
    assert truncated < median_after * coverage.THIN_FRACTION


def test_an_archive_that_is_only_an_in_progress_hour():
    """First hour of a fresh recording: nothing complete to compare against."""
    now = datetime(2026, 9, 24, 16, 3, tzinfo=timezone.utc)
    sizes = {_key(now, 0): 400_000}

    settled, in_progress = coverage.split_in_progress(sizes, now=now)

    assert settled == {}
    assert in_progress == _key(now, 0)
    assert coverage.classify(settled) == ([], [], [], 0.0)
    assert coverage.longest_runs([]) == []


# --------------------------------------------------------------------------
# _file_size
# --------------------------------------------------------------------------

def test_file_size_matches_stat_for_an_ordinary_file(tmp_path):
    path = _write(tmp_path, "20260924T10", 4096)
    assert coverage._file_size(path) == path.stat().st_size == 4096


def test_file_size_ignores_a_stale_directory_entry(tmp_path, monkeypatch):
    """The Windows failure this guards against, constructed directly.

    A stale parent-directory entry reports a size that was true at some point
    in the past - commonly zero, for a file opened moments ago. stat() can
    fall back to reading it. Seeking an open handle cannot.
    """
    path = _write(tmp_path, "20260924T16", 6_800_000)

    class StaleStat:
        st_size = 0

    monkeypatch.setattr(type(path), "stat", lambda self, **kw: StaleStat())

    assert path.stat().st_size == 0, "the stale reading is in place"
    assert coverage._file_size(path) == 6_800_000


def test_hour_sizes_sees_a_file_that_is_still_open_for_writing(tmp_path):
    """The real shape of the problem: a gzip stream with no end marker yet."""
    path = tmp_path / "binance_20260924T16.jsonl.gz"
    fh = gzip.open(path, "wt", encoding="utf-8")
    try:
        assert coverage.hour_sizes(tmp_path) == {"20260924T16": 0}, (
            "freshly opened, nothing flushed"
        )

        for i in range(20_000):
            fh.write('{"stream":"solusdt@aggTrade","data":{"a":%d},"t":1}\n' % i)
        fh.flush()

        on_disk = coverage.hour_sizes(tmp_path)["20260924T16"]
        assert on_disk > 0

        # Whatever the size says, the content is readable up to the tail.
        lines = list(datafile.read_lines(path))
        assert len(lines) > 0
        assert len(list(datafile.read_messages(path))) == len(lines)
    finally:
        fh.close()

    assert coverage.hour_sizes(tmp_path)["20260924T16"] == os.path.getsize(path)


def test_hour_sizes_sums_restart_files_within_an_hour(tmp_path):
    _write(tmp_path, "20260924T16", 3_000_000)
    _write(tmp_path, "20260924T16", 2_000_000, seq=1)
    _write(tmp_path, "20260924T15", FULL)

    assert coverage.hour_sizes(tmp_path) == {
        "20260924T15": FULL,
        "20260924T16": 5_000_000,
    }


# ==========================================================================
# Sub-hour gap detection.
#
# The hour-granularity tests above are about a tool that is wrong on a healthy
# archive. These are about the opposite failure: a tool that is silent on a
# broken one. Every fixture here is synthetic and built in a temp directory -
# the live archive is not reproducible, and a correctness test that depends on
# it is a test that stops meaning anything the moment the recorder restarts.
# ==========================================================================

BASE = datetime(2026, 9, 22, 14, 0, 0, tzinfo=timezone.utc)
BASE_MS = int(BASE.timestamp() * 1000)

# Coarser than the real 100ms depth cadence, purely so the fixtures stay
# small. Gap detection compares intervals against a threshold and does not
# care what the nominal cadence is; the only requirement is that the cadence
# sits well under the threshold, exactly as it does in the archive.
CADENCE_MS = 500

SYMBOLS = ["solusdt", "avaxusdt", "injusdt"]


def _at(seconds: float) -> int:
    """Milliseconds, `seconds` after the base instant."""
    return BASE_MS + int(seconds * 1000)


def _hour_of(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime(HOUR_FMT)


def _beat(symbol: str, ts_ms: int) -> dict:
    """A depth20 snapshot: no exchange timestamp, so `t` is its only clock.

    lastUpdateId is derived from the timestamp so that the *same* snapshot
    written into two overlapping files carries the same identity, which is
    what datafile.message_id deduplicates on.
    """
    return {
        "t": ts_ms,
        "stream": f"{symbol}@depth20@100ms",
        "data": {"lastUpdateId": ts_ms,
                 "bids": [["1.00", "10.0"]], "asks": [["1.01", "10.0"]]},
    }


def _trade(symbol: str, ts_ms: int) -> dict:
    return {
        "t": ts_ms,
        "stream": f"{symbol}@aggTrade",
        "data": {"e": "aggTrade", "E": ts_ms, "s": symbol.upper(), "a": ts_ms,
                 "p": "1.00", "q": "5.0", "f": 1, "l": 2, "m": False, "M": True},
    }


def _beats(symbols, spans, cadence_ms=CADENCE_MS) -> list[dict]:
    """Depth snapshots for `symbols` across `spans`, given as (start_s, end_s)."""
    out = []
    for symbol in symbols:
        for start_s, end_s in spans:
            ts = _at(start_s)
            end = _at(end_s)
            while ts < end:
                out.append(_beat(symbol, ts))
                ts += cadence_ms
    return out


def _write_file(tmp_path, hour: str, messages, seq: int | None = None):
    suffix = "" if seq is None else f".{seq}"
    path = tmp_path / f"binance_{hour}{suffix}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for msg in sorted(messages, key=lambda m: m["t"]):
            fh.write(json.dumps(msg) + "\n")
    return path


def _capture(tmp_path, messages):
    """Write messages the way the recorder does: one file per wall-clock hour."""
    by_hour = collections.defaultdict(list)
    for msg in messages:
        by_hour[_hour_of(msg["t"])].append(msg)
    return [_write_file(tmp_path, hour, msgs)
            for hour, msgs in sorted(by_hour.items())]


def _scan(tmp_path, **kw):
    return coverage.deep_scan(tmp_path, **kw)


# --------------------------------------------------------------------------
# The structural blindness this exists to close
# --------------------------------------------------------------------------

def test_an_hour_missing_a_third_of_itself_still_passes_the_size_test(tmp_path):
    """The whole premise, demonstrated end to end on files we built.

    Three hours. The middle one is missing 23 minutes. It is nowhere near
    THIN_FRACTION - it keeps most of its usual bytes - so the size test calls
    it good and longest_runs() offers all three hours as a replay candidate.
    Tightening THIN_FRACTION is not the fix: the hole is 38% of the hour and
    the threshold is 25%, so catching it on bytes would mean failing every
    hour that is merely quiet. The fix is to stop asking about bytes.
    """
    hole_start, hole_end = 3600 + 600, 3600 + 600 + 23 * 60
    _capture(tmp_path, _beats(SYMBOLS, [(0, hole_start), (hole_end, 3 * 3600)]))

    sizes = coverage.hour_sizes(tmp_path)
    good, thin, missing, _ = coverage.classify(sizes)
    assert thin == [] and missing == []
    assert len(good) == 3, "the holed hour passes on size, as expected"

    runs = coverage.longest_runs(good)
    assert runs[0][2] == 3, "and is offered as part of a 3h replay window"

    # What the timestamps say.
    scan = _scan(tmp_path)
    outages = scan.outages()
    assert len(outages) == 1
    assert outages[0].symbols == tuple(sorted(SYMBOLS))
    assert 23 * 60 <= outages[0].seconds <= 23 * 60 + CADENCE_MS / 1000

    # And the link between the two: the run is no longer offered blind.
    holes = coverage.holes_in_run(runs[0][0], runs[0][1], scan.gaps())
    assert len(holes) == len(SYMBOLS)

    best = scan.replay_windows()[0]
    assert best.seconds < 3 * 3600
    assert abs(best.start_ms - _at(hole_end)) <= CADENCE_MS


# --------------------------------------------------------------------------
# Gap detection
# --------------------------------------------------------------------------

def test_a_clean_contiguous_run_has_no_gaps(tmp_path):
    _capture(tmp_path, _beats(SYMBOLS, [(0, 2 * 3600)]))

    scan = _scan(tmp_path)

    assert scan.gaps() == []
    assert scan.outages() == []
    assert scan.symbols == sorted(SYMBOLS)
    windows = scan.replay_windows()
    assert len(windows) == 1
    assert windows[0].seconds == pytest.approx(2 * 3600, abs=1)


def test_a_single_hole_is_found_and_measured(tmp_path):
    _capture(tmp_path, _beats(SYMBOLS, [(0, 900), (1200, 3600)]))

    scan = _scan(tmp_path)

    for symbol in SYMBOLS:
        gaps = scan.timelines[symbol].gaps
        assert len(gaps) == 1
        assert gaps[0].symbol == symbol
        # Measured between the last message before the hole and the first
        # after it, so it is exactly the silence that was observed: one
        # cadence longer than the 300s of beats that were left out. The gap
        # is what the data shows, not what the fixture intended.
        assert 300 <= gaps[0].seconds <= 300 + CADENCE_MS / 1000


def test_jitter_below_the_threshold_is_not_a_gap(tmp_path):
    """A websocket reconnect is a few seconds. It is not a capture failure."""
    _capture(tmp_path, _beats(SYMBOLS, [(0, 600), (605, 1200)]))

    scan = _scan(tmp_path)

    assert scan.gaps() == []
    # ...but the headroom is measured and reported, not assumed.
    assert scan.timelines["solusdt"].widest_ok_ms == 5000 + CADENCE_MS


def test_the_threshold_is_configurable_and_actually_applied(tmp_path):
    _capture(tmp_path, _beats(SYMBOLS, [(0, 600), (605, 1200)]))

    assert _scan(tmp_path, threshold_s=60).gaps() == []
    assert len(_scan(tmp_path, threshold_s=2).gaps()) == len(SYMBOLS)


def test_a_hole_spanning_an_hour_boundary_is_one_gap(tmp_path):
    """Neither hour is short enough to look wrong on its own.

    The hole runs 14:58 to 15:03, so hour 14 keeps 58 of its 60 minutes and
    hour 15 keeps 57 of its own. Per-hour reasoning of any kind misses this;
    only a timeline that carries across the file boundary sees it.
    """
    _capture(tmp_path, _beats(SYMBOLS, [(0, 58 * 60), (63 * 60, 2 * 3600)]))

    assert sorted(coverage.hour_sizes(tmp_path)) == ["20260922T14", "20260922T15"]

    scan = _scan(tmp_path)
    outages = scan.outages()

    assert len(outages) == 1
    assert 300 <= outages[0].seconds <= 300 + CADENCE_MS / 1000
    assert _hour_of(outages[0].start_ms) == "20260922T14"
    assert _hour_of(outages[0].end_ms) == "20260922T15"


def test_an_hour_with_no_file_at_all_shows_up_as_one_long_gap(tmp_path):
    _capture(tmp_path, _beats(SYMBOLS, [(0, 3600), (2 * 3600, 3 * 3600)]))

    scan = _scan(tmp_path)
    outages = scan.outages()

    assert len(outages) == 1
    assert outages[0].seconds == pytest.approx(3600, abs=CADENCE_MS / 1000 + 1)


# --------------------------------------------------------------------------
# Per symbol: a stream problem is not a capture outage
# --------------------------------------------------------------------------

def test_a_hole_in_one_symbol_is_not_a_capture_outage(tmp_path):
    """One pair dark while the others flow means the exchange stopped sending
    that pair, not that the laptop slept. Same shape in the data, different
    cause, and the report has to keep them apart.
    """
    messages = _beats(["avaxusdt", "injusdt"], [(0, 3600)])
    messages += _beats(["solusdt"], [(0, 1200), (1500, 3600)])
    _capture(tmp_path, messages)

    scan = _scan(tmp_path)
    outages = scan.outages()

    assert len(outages) == 1
    assert outages[0].symbols == ("solusdt",)
    assert outages[0].kind(len(SYMBOLS)) == "stream gap"
    assert scan.timelines["avaxusdt"].gaps == []
    assert scan.timelines["injusdt"].gaps == []


def test_all_symbols_dark_together_is_a_capture_outage(tmp_path):
    _capture(tmp_path, _beats(SYMBOLS, [(0, 1200), (1500, 3600)]))

    outages = _scan(tmp_path).outages()

    assert len(outages) == 1, "simultaneous per-symbol gaps are one event"
    assert outages[0].symbols == tuple(sorted(SYMBOLS))
    assert outages[0].kind(len(SYMBOLS)) == "capture outage"


def test_symbols_do_not_have_to_drop_on_the_same_millisecond(tmp_path):
    """They never do. Grouping is by overlap, not by equality."""
    messages = []
    for i, symbol in enumerate(SYMBOLS):
        messages += _beats([symbol], [(0, 1200 + i * 0.5), (1500 + i * 0.5, 3600)])
    _capture(tmp_path, messages)

    outages = _scan(tmp_path).outages()

    assert len(outages) == 1
    assert outages[0].symbols == tuple(sorted(SYMBOLS))


def test_a_silent_trade_stream_is_not_a_gap(tmp_path):
    """Several of these pairs genuinely go minutes without a trade.

    If trades counted as a heartbeat, a quiet market would read as a capture
    failure - which is the same conflation of thin and missing that the
    module docstring is about, just moved down a level.
    """
    messages = _beats(SYMBOLS, [(0, 3600)])
    messages += [_trade("solusdt", _at(t)) for t in (0, 5, 3000, 3500)]
    _capture(tmp_path, messages)

    scan = _scan(tmp_path)

    assert scan.gaps() == []
    assert scan.counts["20260922T14"]["solusdt"] > scan.timelines["solusdt"].beats


# --------------------------------------------------------------------------
# Restart and duplicate files: the way to manufacture a gap that is not there
# --------------------------------------------------------------------------

def test_a_restart_file_is_a_continuation_not_a_gap(tmp_path):
    """binance_...T14.jsonl.gz then binance_...T14.1.jsonl.gz.

    Plain string sort puts ".1" first, which would present the hour's second
    half before its first and read as a gap the size of the hour. Only
    datafile._sort_key gets this right.
    """
    _write_file(tmp_path, "20260922T14", _beats(SYMBOLS, [(0, 1800)]))
    _write_file(tmp_path, "20260922T14", _beats(SYMBOLS, [(1800, 3600)]), seq=1)

    scan = _scan(tmp_path)

    assert scan.gaps() == []
    assert scan.replay_windows()[0].seconds == pytest.approx(3600, abs=1)


def test_a_duplicate_file_does_not_manufacture_a_gap(tmp_path):
    """Two recorders running at once: the same hour written twice.

    Every message arrives a second time with the same identity. Deduplication
    has to happen before the timeline is built, and a timestamp already behind
    the running edge has to be treated as covered rather than as time going
    backwards.
    """
    messages = _beats(SYMBOLS, [(0, 3600)])
    _write_file(tmp_path, "20260922T14", messages)
    _write_file(tmp_path, "20260922T14", messages, seq=1)

    scan = _scan(tmp_path)

    assert scan.gaps() == []
    expected = len(messages) // len(SYMBOLS)
    assert scan.timelines["solusdt"].beats == expected, "counted once, not twice"
    assert scan.counts["20260922T14"]["solusdt"] == expected


def test_a_restart_file_can_fill_the_first_files_hole(tmp_path):
    """The case that proves gaps are computed after the merge, not per file.

    The original file has a ten-minute hole; the restart file covers the whole
    hour. Looked at one file at a time there is a gap. Looked at the union -
    which is what the hour actually contains - there is not.
    """
    _write_file(tmp_path, "20260922T14",
                _beats(SYMBOLS, [(0, 1200), (1800, 3600)]))
    _write_file(tmp_path, "20260922T14", _beats(SYMBOLS, [(0, 3600)]), seq=1)

    scan = _scan(tmp_path)

    assert scan.gaps() == []
    assert scan.timelines["solusdt"].beats == 3600 * 1000 // CADENCE_MS


def test_a_restart_file_covering_part_of_the_hole_leaves_the_rest(tmp_path):
    """And the same merge must not paper over what is still missing."""
    _write_file(tmp_path, "20260922T14",
                _beats(SYMBOLS, [(0, 1200), (2400, 3600)]))
    _write_file(tmp_path, "20260922T14", _beats(SYMBOLS, [(1200, 1500)]), seq=1)

    outages = _scan(tmp_path).outages()

    assert len(outages) == 1
    assert outages[0].seconds == pytest.approx(900, abs=CADENCE_MS / 1000 + 1)


# --------------------------------------------------------------------------
# Replay window selection - the part that is actually the deliverable
# --------------------------------------------------------------------------

def test_replay_windows_are_the_intersection_across_symbols(tmp_path):
    """A window is only replayable if every symbol is unbroken across it.

    The demo shows every pair side by side. A window where one is dark is a
    window with a blank panel, which on stage looks like a broken detector.
    """
    messages = _beats(["injusdt"], [(0, 3600)])
    messages += _beats(["solusdt"], [(0, 600), (900, 3600)])
    messages += _beats(["avaxusdt"], [(0, 2000), (2300, 3600)])
    _capture(tmp_path, messages)

    scan = _scan(tmp_path)
    windows = scan.replay_windows()

    assert scan.timelines["injusdt"].gaps == [], "injusdt alone is unbroken"
    assert [round(w.seconds) for w in windows] == [1300, 1100, 600]
    for win in windows:
        assert coverage.verify_window(win.start_ms, win.end_ms, tmp_path) == {}, (
            "every offered window survives its own verification"
        )


def test_verify_window_rejects_a_window_containing_a_hole(tmp_path):
    _capture(tmp_path, _beats(SYMBOLS, [(0, 1200), (1500, 3600)]))

    bad = coverage.verify_window(_at(600), _at(2400), tmp_path)

    assert sorted(bad) == sorted(SYMBOLS)
    assert all(len(holes) == 1 for holes in bad.values())


def test_verify_window_accepts_a_clean_window(tmp_path):
    _capture(tmp_path, _beats(SYMBOLS, [(0, 1200), (1500, 3600)]))

    assert coverage.verify_window(_at(1500), _at(3000), tmp_path) == {}


def test_verify_window_counts_an_uncovered_edge(tmp_path):
    """A window starting before the data does is blank screen, not a clean run."""
    _capture(tmp_path, _beats(SYMBOLS, [(600, 3600)]))

    bad = coverage.verify_window(_at(0), _at(3000), tmp_path)

    assert sorted(bad) == sorted(SYMBOLS)
    assert bad["solusdt"][0].start_ms == _at(0)


def test_verify_window_reads_only_the_hours_it_needs(tmp_path):
    """It is the guard in front of an expensive scan, so it has to be cheap."""
    _capture(tmp_path, _beats(SYMBOLS, [(0, 4 * 3600)]))

    assert coverage.hours_covering(_at(3700), _at(3800)) == ["20260922T15"]
    assert coverage.hours_covering(_at(3500), _at(3700)) == [
        "20260922T14", "20260922T15",
    ]
    assert coverage.verify_window(_at(3700), _at(3800), tmp_path) == {}


def test_a_restricted_scan_knows_which_hours_it_did_not_read(tmp_path):
    """Unknown must not be able to render as clean anywhere downstream."""
    _capture(tmp_path, _beats(SYMBOLS, [(0, 3 * 3600)]))

    scan = _scan(tmp_path, hours=["20260922T15"])

    assert scan.hours == ("20260922T15",)
    assert set(scan.counts) == {"20260922T15"}


def test_an_hour_that_decodes_to_nothing_is_told_apart_from_a_capture_gap(tmp_path):
    """A file that is the right size and will not decompress.

    Found by running this against the live archive: ten hour-files of 5-7 MB
    whose deflate stream breaks a few hundred lines in. read_lines tolerates
    it by design - a truncated tail is the normal state of a file still being
    written - so nothing raises, the size test sees megabytes, and the hour
    reads as good while containing nothing.

    The gap scan catches it regardless, as an hour-long hole. But "capture
    outage" points at the laptop and this one points at the disk, so the two
    have to be told apart.
    """
    _capture(tmp_path, _beats(SYMBOLS, [(0, 3600), (2 * 3600, 3 * 3600)]))

    head = b"".join(
        json.dumps(_beat("solusdt", _at(3600 + i))).encode() + b"\n"
        for i in range(50)
    )
    corrupt = tmp_path / "binance_20260922T15.jsonl.gz"
    corrupt.write_bytes(gzip.compress(head) + os.urandom(500_000))

    sizes = coverage.hour_sizes(tmp_path)
    good, thin, missing, median = coverage.classify(sizes)
    assert "20260922T15" in good, "it passes the size test, which is the point"

    scan = coverage.deep_scan(tmp_path)
    broken = coverage.decoded_but_empty(
        sizes, scan, median * coverage.THIN_FRACTION
    )

    assert [hour for hour, _, _ in broken] == ["20260922T15"]
    assert broken[0][2] == 50, "the readable prefix, and nothing after it"
    # ...and it is still an hour of darkness, reported as one.
    assert scan.outages()[0].seconds == pytest.approx(3600, abs=2)


def test_a_merely_quiet_hour_is_not_called_a_decode_failure(tmp_path):
    """The 1% margin has to be wide enough that no real market reaches it."""
    messages = _beats(SYMBOLS, [(0, 3600), (2 * 3600, 3 * 3600)])
    messages += _beats(SYMBOLS, [(3600, 2 * 3600)], cadence_ms=CADENCE_MS * 20)
    _capture(tmp_path, messages)

    sizes = coverage.hour_sizes(tmp_path)
    _, _, _, median = coverage.classify(sizes)
    scan = coverage.deep_scan(tmp_path)

    assert scan.gaps() == [], "a 10s cadence is quiet, not missing"
    assert coverage.decoded_but_empty(
        sizes, scan, median * coverage.THIN_FRACTION
    ) == []


def test_verify_window_does_not_call_a_symbol_clean_by_omitting_it(tmp_path):
    """A symbol absent from the window has no timeline to inspect.

    Left alone, that reads as no gaps found, which is the one answer a gap
    check must never give for a stream that is not there at all. Naming the
    symbols you need is what makes absence visible.
    """
    _capture(tmp_path, _beats(["avaxusdt", "injusdt"], [(0, 3600)]))

    assert coverage.verify_window(_at(0), _at(3000), tmp_path) == {}, (
        "nothing to say about a symbol it was never told to expect"
    )

    bad = coverage.verify_window(_at(0), _at(3000), tmp_path, symbols=SYMBOLS)

    assert list(bad) == ["solusdt"]
    assert bad["solusdt"][0].start_ms == _at(0)
    assert bad["solusdt"][0].end_ms == _at(3000)


def test_verify_window_on_a_window_holding_nothing_at_all(tmp_path):
    _capture(tmp_path, _beats(SYMBOLS, [(0, 3600)]))

    bad = coverage.verify_window(_at(5 * 3600), _at(6 * 3600), tmp_path)

    assert list(bad) == [coverage.NOTHING]
    assert bad[coverage.NOTHING][0].seconds == 3600
