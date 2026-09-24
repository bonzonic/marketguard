"""Tests for the in-progress hour, which is the only part of coverage.py that
can be wrong about a healthy archive.

Everything here builds its own files in a temp directory. Nothing touches the
real recording - the archive is not reproducible and a test is not a good
enough reason to have a process reading it in a loop.

    python -m pytest test_coverage.py -q
"""
import gzip
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
