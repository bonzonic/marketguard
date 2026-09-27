"""The spoofing gate: cheap statistics that nominate windows, never convict.

This is the first of the two detectors described in the README, and the only
one the recorded archive can currently support - there is no pump in it, but
there is plenty of large, short-lived, near-mid resting size.

The frozen design is a strict conjunction::

    wall > p99  AND  within 20bps of mid  AND  cancelled < 500ms
                AND  >= 3 occurrences in 90s

Everything below is about the difference between implementing that sentence
and implementing something that *looks* like it while being an artifact
generator. Four of those differences are large enough that a naive reading
produces confident nonsense, so they get their own sections.

**1. "wall > p99" is very nearly free, and cannot carry the conjunction.**

``p99`` in features.py is a percentile over single *level observations*
inside a 5bps price band. A one-minute bar holds on the order of a thousand
such observations, so roughly ten of them clear their own p99 by
construction. Measured across 51h of solusdt, 80% of bars contain at least
one p99 exceedance and the median bar contains fourteen. A conjunct that is
true in 80% of bars is not a conjunct, it is a formality.

features.py already provides the fix: :attr:`FeatureConfig.wall_flag_ratio`,
size as a *multiple* of the band p99, default 3.0, which flags 5.3% of bars
instead of 80%. This module implements the wall conjunct as
``max_usd >= wall_ratio * band_p99`` and keeps the plain exceedance only as
an audit counter. Measured on solusdt's busiest recorded hour (09-23 14:00),
of 825 episodes that were cancelled, near mid, and under 500ms:

    * 217 cleared plain p99  (one every 17 seconds - useless)
    * 0 cleared 3x band p99

That ratio is the entire difference between a detector and an alarm that
never stops. Reproduce it with ``python spoofing.py --known-wall`` and the
funnel line ``python spoofing.py solusdt 1`` prints.

**2. ``out_of_book`` is not a cancellation, and neither is band drift.**

The feed shows twenty levels. A resting level stops being visible for three
different reasons and only one of them is a cancellation:

    * somebody cancelled it                          -> ``CANCELLED``
    * it was traded through                          -> ``FILLED``/``PARTIAL``
    * price moved and it fell off the bottom of the
      twenty-level window                            -> ``OUT_OF_BOOK``

Measured on one hour of solusdt, 3,191 of 16,443 episodes (19%) are
``OUT_OF_BOOK``. Counting them as cancels inflates the cancel rate by about a
quarter - and cancel rate plus short lifetime *is* the spoofing signal, so a
symbol whose mid simply trended would be reported as having been spoofed the
whole way up. This module gates on ``outcome is CANCELLED`` and nothing else.

There is a fourth departure that features.py does not label, because from its
point of view it is indistinguishable from a cancel: a level can leave the
*proximity band* while remaining in the book. ``level_episodes`` only tracks
levels within ``level_max_bps`` of mid, so when mid rises far enough that a
resting bid is 21bps away, the level drops out of the tracked set, is still
at or above the visible bid floor, has no matching trades, and is therefore
recorded as ``CANCELLED``. Nobody cancelled anything. This is the same error
as ``OUT_OF_BOOK`` wearing a different hat, and it is worse on the wide-tick
symbols where twenty levels span far more than 20bps (opusdt averages an
8.0bps *spread*). :meth:`SpoofingDetector.events` therefore re-measures the
level's distance from mid at the instant it vanished and rejects the episode
as ``BAND_DRIFT`` if it had already left the band.

Measured across the whole archive, this is not a rounding error and the two
departures are near-perfectly complementary, because which one fires depends
on whether twenty levels span more or less than 20bps:

    symbol      out_of_book     band_drift   of all episodes
    solusdt         34,981               1        86,200
    avaxusdt         2,123               8        31,527
    seiusdt            386          44,820       134,993
    injusdt          4,367          77,153       169,110
    opusdt               0           8,722        19,325
    arbusdt              1          16,512        35,481

On solusdt (0.85bps average spread) the book is far narrower than the
proximity band, so a level always falls out of the window first. On arbusdt
(5.0bps) and opusdt (8.0bps) it is the other way round and *half of every
departure* is band drift. Without this check those two symbols would report
roughly 16,500 and 8,700 cancellations that nobody performed.

**3. Depth is not comparable across snapshots unless the price is unchanged.**

Inherited rather than re-derived: ``level_episodes`` keys episodes on
``(side, price)``, which is the general form of the ``best_bid = prev_bid``
constraint. A ``lag(best_bid_qty)`` comparison across a top-of-book that
flickers between adjacent ticks carrying tiny sizes produces enormous
multipliers that are pure artifact and look exactly like dramatic findings.
Nothing in this module ever compares sizes at two different prices.

**4. One wall that wobbles is not three spoofs.**

The ">= 3 occurrences in 90s" conjunct is the one that promotes a single
event into a candidate, so anything that splits one placement into several
episodes attacks it directly. ``level_episodes`` opens an episode when size
at a price crosses ``level_min_usd`` and closes it when it falls back below,
so a wall hovering near that floor fragments into a burst of episodes at the
same price, seconds apart, each looking like a fresh placement. The real
09-23 14:53 wall fragmented into three episodes for exactly this reason (it
kept dropping out of the visible window and coming back).

:func:`coalesce_episodes` therefore stitches consecutive episodes at the same
``(side, price)`` back together when the gap between them is at most
``coalesce_gap_ms`` (default 200ms, two book updates). A genuine
cancel-and-replace inside 200ms is possible and this will merge it; that
costs recall, and the precision-first stance resolves the ambiguity toward
"one event" deliberately.

**Why episodes and not features.walls().**

``features.walls()`` evaluates the book on a 1/second grid, because resting
size is enormously autocorrelated and ten snapshots per second say the same
thing at ten times the cost. That is correct for the wall *feature* and
useless here: a 400ms spoof has well under an even chance of appearing in any
one-second sample at all. The lifetimes this detector is defined by can only
be seen at full snapshot resolution, which is what ``level_episodes``
provides. The band p99 reference is still built from the 1/second grid - a
percentile wants a distribution, not every autocorrelated repeat of it.

**Why the p99 reference is frozen per chunk.**

features.py re-evaluates the trailing p99 for every observation with a
windowed ``quantile_cont``. At one-second sampling over an hour that is
affordable. At full snapshot resolution over five days it is O(n * window)
and it is not. The reference here is computed once per chunk (default one
hour) from the six hours *strictly preceding* that chunk, and held fixed
across it. A six-hour trailing percentile does not move meaningfully inside
one hour, the window never reaches forward, and the estimator matches
features.py's: on 09-23 14:00 solusdt this module's reference has 129,600
observations against features.py's 129,592.

**The archive's best wall does not pass this gate, and should not.**

The strongest wall in the capture - solusdt 09-23 14:53, $3.44M resting
3.1bps from mid at 16.9x its band p99 - fails two conjuncts, and the second
failure is the interesting one:

    * it rested for 42 seconds, which is 84x the 500ms limit; and
    * it is not recorded as cancelled at all. It is ``OUT_OF_BOOK``: price
      rallied away from the bid until the level fell off the bottom of the
      twenty-level window, so the archive simply does not say whether anyone
      ever cancelled it.

The frozen thresholds describe a fast spoofer and the archive's best
specimen is a slow one. Widening ``max_lifetime_ms`` until it fires would be
fitting the threshold to the single example anybody has looked at, and the
second failure means even that would not work. It is reported here as a
finding rather than patched away; see :func:`known_wall_report`.

**Candidates, not verdicts.** Everything this module emits is an invitation
for the LLM cascade to look. Nothing here establishes intent, and intent is
what distinguishes spoofing from a market maker managing inventory quickly.

Usage:
    python spoofing.py                    # every symbol, last 24h
    python spoofing.py solusdt 120        # one symbol, 120 hours
    python spoofing.py --hours=200        # every symbol, the whole archive
    python spoofing.py --known-wall       # the 09-23 14:53 wall, conjunct by conjunct
    python -m pytest test_spoofing.py
"""
from __future__ import annotations

import datetime
import os
import statistics
import sys
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Sequence

import duckdb

import config
import features as F
import query
from features import LevelEpisode, Outcome

HOUR_MS = 3_600_000


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SpoofConfig:
    """The frozen conjunction, plus the guards that keep it honest.

    The four gate thresholds (``wall_ratio``, ``proximity_bps``,
    ``max_lifetime_ms``, ``min_occurrences`` / ``cluster_window_ms``) are the
    design as frozen and are not tuned to the archive. Everything else is
    machinery, and where machinery had a judgement call the reasoning is on
    the field.
    """

    # --- the conjunction -------------------------------------------------
    # "wall > p99", implemented as a multiple of the band p99 rather than a
    # bare exceedance. See section 1 of the module docstring: at 1.0 this
    # conjunct is true for ~274 episodes an hour on solusdt and contributes
    # nothing. Defaults to features.FeatureConfig.wall_flag_ratio so the two
    # modules cannot drift apart silently.
    wall_ratio: float = F.FeatureConfig.wall_flag_ratio
    proximity_bps: float = 20.0
    max_lifetime_ms: int = 500
    min_occurrences: int = 3
    cluster_window_ms: int = 90_000

    # --- grouping --------------------------------------------------------
    # The frozen sentence says ">= 3 occurrences", not ">= 3 on the same
    # side". Requiring one side is a *narrowing* of the frozen design and is
    # flagged as such: a quote-stuffer flickering both sides during a
    # volatility burst produces mixed-side bursts that look like repetition
    # and are not, while a spoofer pushing price works one side. Precision
    # over recall says group by side; set False to get the literal reading,
    # and the diagnostic prints both so the difference is never invisible.
    cluster_by_side: bool = True

    # --- outcome handling ------------------------------------------------
    # PARTIAL means "some aggressive volume crossed this level's price while
    # it rested", which is not the same as "this order was hit". The
    # attribution in features.matched_qty is deliberately generous - it counts
    # every aggressive trade at or through the price, not only volume that
    # provably consumed *this* order - so PARTIAL is over-produced and
    # CANCELLED is under-produced. Both errors point the same way: excluding
    # PARTIAL costs recall and cannot manufacture a candidate. A spoofer whose
    # wall gets nicked by one small taker before they pull it is therefore
    # missed. That is accepted; it is the cheaper mistake.
    include_partial: bool = False

    # --- fragmentation guard ---------------------------------------------
    # See section 4. Expressed in ms but meaning "at most two missing
    # snapshots": the measured inter-message gap for @depth20@100ms is a
    # median of 100ms, and the jitter around that is real. A literal 200ms
    # is not enough - the two halves of the archive's biggest wall are 201ms
    # apart and a 200ms threshold reports them as two placements. 300ms
    # covers two missed snapshots plus jitter. Merging errs toward "one
    # event", which can only reduce candidates, never manufacture one.
    coalesce_gap_ms: int = 300

    # --- band p99 reference ----------------------------------------------
    # Mirrors features.FeatureConfig so the two estimators agree by
    # construction rather than by coincidence.
    band_bps: float = F.FeatureConfig.wall_band_bps
    max_band_bps: float = F.FeatureConfig.wall_max_bps
    ref_sample_ms: int = F.FeatureConfig.wall_sample_ms
    ref_ms: int = F.FeatureConfig.wall_ref_ms
    quantile: float = F.FeatureConfig.wall_quantile
    min_ref_obs: int = F.FeatureConfig.wall_min_ref_obs

    # --- chunking --------------------------------------------------------
    # Episode tracking needs every snapshot, so it runs in bounded chunks.
    # An episode straddling a chunk boundary is split - the head is reported
    # OPEN and the tail starts fresh - which loses it. At a 500ms lifetime
    # cap the chance of straddling an hour boundary is 500/3_600_000, about
    # one in seven thousand, so this is left alone rather than papered over
    # with an overlap that would need its own deduplication. Clustering is
    # done once over the whole range, never per chunk, so a burst spanning a
    # boundary is still found.
    chunk_ms: int = HOUR_MS

    # --- tracking floor ---------------------------------------------------
    # features.FeatureConfig.level_min_usd is absolute and its docstring says
    # plainly that $50k is a routine level on solusdt and an hourly event on
    # avaxusdt. Tracking every episode above an absolute floor across a
    # watchlist is therefore either unaffordable or blind depending on the
    # symbol. Instead the floor is derived per chunk from that chunk's own
    # band p99 table: an episode can only qualify at ``wall_ratio * p99``, so
    # anything below that can be dropped for free.
    #
    # The factor exists because the floor is not free: ``level_episodes``
    # closes an episode the moment size dips under it, so a floor set exactly
    # at the qualifying threshold would shred every wall that hovers there
    # into fragments and feed the repetition conjunct (section 4). At 0.5 a
    # qualifying wall has to halve before it fragments.
    track_frac: float = 0.5

    # est:1 timestamps are ~22ms median / 67ms p90 off, against a 500ms
    # lifetime conjunct. That is a 13% error at p90 on the quantity the gate
    # turns on, so they are excluded. 12% of the archive's snapshots are
    # reconstructed.
    exclude_estimated: bool = True

    def __post_init__(self) -> None:
        if self.wall_ratio < 1.0:
            raise ValueError("wall_ratio < 1.0 is weaker than the p99 conjunct itself")
        if self.min_occurrences < 1:
            raise ValueError("min_occurrences must be >= 1")
        if self.cluster_window_ms < 1 or self.chunk_ms < 1:
            raise ValueError("cluster_window_ms and chunk_ms must be positive")
        if not 0.0 < self.track_frac <= 1.0:
            raise ValueError("track_frac must be in (0, 1]")
        if self.max_lifetime_ms < 1:
            raise ValueError("max_lifetime_ms must be >= 1")

    def feature_config(self, level_min_usd: float) -> F.FeatureConfig:
        """The FeatureConfig this gate needs from the extractor.

        ``level_max_bps`` is pinned to ``proximity_bps`` so the "within 20bps
        of mid" conjunct is enforced by the episode tracker itself - every
        snapshot of every tracked episode was inside the band. What the
        tracker cannot enforce is *leaving* the band, which is what the
        BAND_DRIFT check exists for.
        """
        return F.FeatureConfig(
            level_min_usd=level_min_usd,
            level_max_bps=self.proximity_bps,
            level_exclude_estimated=self.exclude_estimated,
            wall_band_bps=self.band_bps,
            wall_max_bps=self.max_band_bps,
            wall_sample_ms=self.ref_sample_ms,
            wall_ref_ms=self.ref_ms,
            wall_quantile=self.quantile,
            wall_min_ref_obs=self.min_ref_obs,
            wall_flag_ratio=self.wall_ratio,
            wall_proximity_bps=self.proximity_bps,
        )


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------

class Reject(str, Enum):
    """Why an episode did not become an event.

    Kept as an explicit enum and counted, rather than being expressed as a
    chain of ``continue`` statements, because the funnel *is* the evidence
    that this gate is selective. A detector that reports only what fired
    cannot be told apart from one that fires on everything, and "crying wolf
    is worse than nothing" is this project's stated top risk.
    """

    NO_REFERENCE = "no_reference"        # no trailing p99 for the band
    THIN_REFERENCE = "thin_reference"    # fewer than min_ref_obs behind it
    NO_MID = "no_mid"                    # snapshot clock gap; cannot measure bps
    OUT_OF_BOOK = "out_of_book"          # fell off the 20-level window
    BAND_DRIFT = "band_drift"            # left the proximity band, still in book
    FILLED = "filled"
    PARTIAL = "partial"
    OPEN = "open"                        # right-censored by the chunk edge
    TOO_LONG = "too_long"                # lifetime >= max_lifetime_ms
    BELOW_P99 = "below_p99"              # not even a plain p99 exceedance
    BELOW_RATIO = "below_ratio"          # cleared p99, under wall_ratio x p99


@dataclass(frozen=True)
class BandRef:
    """The trailing p99 for one ``(side, band)``, and how much is behind it."""

    side: str
    band: int
    p99: float
    ref_n: int

    @property
    def usable(self) -> bool:
        return self.p99 > 0 and self.ref_n > 0


@dataclass(frozen=True)
class SpoofEvent:
    """One placement that passed every per-episode conjunct.

    Not a spoof. A large near-mid order that was cancelled quickly, which is
    also what a market maker does when the quote moves. Only repetition -
    :class:`SpoofCandidate` - makes it worth an LLM's attention.
    """

    symbol: str
    side: str
    price: float
    first_t: int
    last_t: int
    max_usd: float
    min_bps: float          # closest it came to mid while resting
    end_bps: float          # distance from mid at the instant it vanished
    bands: tuple[int, ...]  # every 5bps band it occupied
    band_p99: float         # the most conservative p99 among those bands
    ratio: float            # max_usd / band_p99
    ref_n: int

    @property
    def lifetime_ms(self) -> int:
        return self.last_t - self.first_t

    @property
    def above_plain_p99(self) -> bool:
        """True whenever ``ratio > 1``.

        Recorded because the frozen design names "wall > p99" as a conjunct.
        It is strictly implied by ``ratio >= wall_ratio`` for any
        ``wall_ratio >= 1``, so it never rejects anything the ratio conjunct
        accepts; it exists to be counted, not to be believed.
        """
        return self.ratio > 1.0


@dataclass(frozen=True)
class SpoofCandidate:
    """``>= min_occurrences`` events inside ``cluster_window_ms``."""

    symbol: str
    side: str | None          # None when cluster_by_side is off
    events: tuple[SpoofEvent, ...]

    @property
    def start_t(self) -> int:
        return self.events[0].first_t

    @property
    def end_t(self) -> int:
        return max(e.last_t for e in self.events)

    @property
    def span_ms(self) -> int:
        return self.end_t - self.start_t

    @property
    def n_events(self) -> int:
        return len(self.events)

    @property
    def max_usd(self) -> float:
        return max(e.max_usd for e in self.events)

    @property
    def max_ratio(self) -> float:
        return max(e.ratio for e in self.events)

    @property
    def min_bps(self) -> float:
        return min(e.min_bps for e in self.events)

    @property
    def median_lifetime_ms(self) -> float:
        return statistics.median([e.lifetime_ms for e in self.events])

    @property
    def n_distinct_prices(self) -> int:
        """How many prices the burst used.

        1 means the same level was placed and pulled repeatedly - the textbook
        pattern, and also what a fragmented single wall looks like if
        :func:`coalesce_episodes` failed to stitch it. Shown in every report
        so a reviewer can tell those apart by eye.
        """
        return len({e.price for e in self.events})

    def summary(self) -> dict:
        """The structured form the LLM cascade receives.

        Deliberately not raw ticks: sizes, distances, lifetimes, counts. See
        the README - the model never sees the book.
        """
        return {
            "symbol": self.symbol,
            "side": self.side,
            "start": _iso(self.start_t),
            "span_ms": self.span_ms,
            "n_events": self.n_events,
            "n_distinct_prices": self.n_distinct_prices,
            "max_usd": round(self.max_usd),
            "max_ratio_vs_band_p99": round(self.max_ratio, 2),
            "closest_bps_from_mid": round(self.min_bps, 2),
            "median_lifetime_ms": self.median_lifetime_ms,
        }


@dataclass(frozen=True)
class GateAudit:
    """Where the episodes went.

    ``n_episodes`` is after coalescing; ``n_coalesced`` counts the episodes
    that were stitched away.
    """

    n_episodes: int = 0
    n_coalesced: int = 0
    n_events: int = 0
    n_above_plain_p99: int = 0
    rejects: dict[str, int] = field(default_factory=dict)
    chunks: int = 0
    chunks_without_reference: int = 0
    track_floor_usd: list[float] = field(default_factory=list)

    def merged(self, other: "GateAudit") -> "GateAudit":
        rejects = dict(self.rejects)
        for k, v in other.rejects.items():
            rejects[k] = rejects.get(k, 0) + v
        return GateAudit(
            n_episodes=self.n_episodes + other.n_episodes,
            n_coalesced=self.n_coalesced + other.n_coalesced,
            n_events=self.n_events + other.n_events,
            n_above_plain_p99=self.n_above_plain_p99 + other.n_above_plain_p99,
            rejects=rejects,
            chunks=self.chunks + other.chunks,
            chunks_without_reference=(self.chunks_without_reference
                                      + other.chunks_without_reference),
            track_floor_usd=self.track_floor_usd + other.track_floor_usd,
        )

    def funnel(self) -> str:
        parts = [f"episodes={self.n_episodes:,}"]
        if self.n_coalesced:
            parts.append(f"coalesced={self.n_coalesced:,}")
        for reason in Reject:
            n = self.rejects.get(reason.value, 0)
            if n:
                parts.append(f"{reason.value}={n:,}")
        parts.append(f"plain_p99={self.n_above_plain_p99:,}")
        parts.append(f"events={self.n_events:,}")
        return "  ".join(parts)


@dataclass(frozen=True)
class ScanResult:
    """Everything one symbol/range produced, including what it rejected."""

    symbol: str
    start: int
    end: int
    events: tuple[SpoofEvent, ...]
    # Everything that passed every conjunct *except* the magnitude one - i.e.
    # the literal frozen "wall > p99". Carried so the gap between the two
    # readings of that conjunct is a number in the output rather than a claim
    # in a docstring; on solusdt it is roughly two orders of magnitude.
    events_above_p99: tuple[SpoofEvent, ...]
    candidates: tuple[SpoofCandidate, ...]
    audit: GateAudit
    hours_scanned: float

    @property
    def events_per_hour(self) -> float:
        return len(self.events) / self.hours_scanned if self.hours_scanned else 0.0

    @property
    def candidates_per_hour(self) -> float:
        return len(self.candidates) / self.hours_scanned if self.hours_scanned else 0.0


# --------------------------------------------------------------------------
# pure logic
#
# These two functions hold the parts of the gate that can be wrong while
# still running happily, so they are dependency-free and separately tested
# against synthetic input rather than against the archive.
# --------------------------------------------------------------------------

def coalesce_episodes(
    episodes: Sequence[LevelEpisode],
    gap_ms: int,
) -> tuple[list[LevelEpisode], int]:
    """Stitch fragments of one resting level back into a single episode.

    Returns ``(episodes, n_merged_away)``.

    Two consecutive episodes at the same ``(side, price)`` separated by at
    most ``gap_ms`` are treated as one placement that momentarily dipped under
    the tracking floor (or out of the visible window and back), not as two.
    The merged episode keeps the first ``first_t``, the last ``last_t``, the
    largest ``max_usd``, the last ``last_usd``, the summed ``filled_qty`` and
    the *last* fragment's outcome - because how the level finally left is the
    only departure that describes the whole placement.

    ``gap_ms = 0`` disables merging (an exactly-zero gap is impossible: the
    successor opens at a strictly later snapshot than the predecessor closed).

    Why this matters: the ">= 3 in 90s" conjunct is the one that promotes an
    event into a candidate, and un-stitched fragments are indistinguishable
    from deliberate repetition at the same price.
    """
    if gap_ms <= 0 or not episodes:
        return list(episodes), 0

    by_key: dict[tuple[str, float], list[LevelEpisode]] = {}
    for ep in episodes:
        by_key.setdefault((ep.side, ep.price), []).append(ep)

    out: list[LevelEpisode] = []
    merged_away = 0
    for group in by_key.values():
        group.sort(key=lambda e: e.first_t)
        current = group[0]
        for nxt in group[1:]:
            if nxt.first_t - current.last_t <= gap_ms:
                current = replace(
                    current,
                    last_t=nxt.last_t,
                    max_usd=max(current.max_usd, nxt.max_usd),
                    last_usd=nxt.last_usd,
                    filled_qty=current.filled_qty + nxt.filled_qty,
                    outcome=nxt.outcome,
                )
                merged_away += 1
            else:
                out.append(current)
                current = nxt
        out.append(current)

    out.sort(key=lambda e: (e.first_t, e.side, e.price))
    return out, merged_away


def cluster_events(
    events: Sequence[SpoofEvent],
    window_ms: int,
    min_occurrences: int,
    by_side: bool = True,
) -> list[SpoofCandidate]:
    """Maximal bursts of ``>= min_occurrences`` events within ``window_ms``.

    The window slides over event *start* times. Overlapping bursts are merged
    into one candidate rather than reported once per qualifying position -
    otherwise five events inside 90s would be announced three times, and a
    surveillance tool that reports the same wall repeatedly is the failure
    mode this project cares most about avoiding.

    Grouping is by ``(symbol, side)`` when ``by_side``; see
    :attr:`SpoofConfig.cluster_by_side` for why the default narrows the
    frozen design.
    """
    if min_occurrences < 1:
        raise ValueError("min_occurrences must be >= 1")

    groups: dict[tuple[str, str | None], list[SpoofEvent]] = {}
    for ev in events:
        key = (ev.symbol, ev.side if by_side else None)
        groups.setdefault(key, []).append(ev)

    out: list[SpoofCandidate] = []
    for (symbol, side), group in groups.items():
        group.sort(key=lambda e: e.first_t)
        starts = [e.first_t for e in group]
        lo = 0
        run: tuple[int, int] | None = None   # inclusive index span
        for hi in range(len(group)):
            # Smallest lo such that the window [starts[hi] - window_ms,
            # starts[hi]] holds every event from lo to hi. lo only ever moves
            # forward, hence the max().
            lo = max(lo, bisect_left(starts, starts[hi] - window_ms))
            if hi - lo + 1 < min_occurrences:
                continue
            if run is not None and lo <= run[1]:
                run = (run[0], hi)           # overlaps the open burst: extend
            else:
                if run is not None:
                    out.append(SpoofCandidate(symbol, side,
                                              tuple(group[run[0]:run[1] + 1])))
                run = (lo, hi)
        if run is not None:
            out.append(SpoofCandidate(symbol, side,
                                      tuple(group[run[0]:run[1] + 1])))

    out.sort(key=lambda c: (c.start_t, c.symbol, c.side or ""))
    return out


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------

# The reference distribution for "is this level unusually large for its
# distance from mid". Sampled at 1/second exactly as features.py samples it -
# resting size is heavily autocorrelated, so the extra nine snapshots per
# second add cost and no information to a percentile. Note ``floor(bps/band)``
# rather than DuckDB's ``//``: ``//`` on DOUBLE operands is ordinary division,
# which would give every level its own float-valued "band" and leave every
# band holding exactly one observation.
_BAND_P99_SQL = """
WITH grid AS (
    SELECT t, mid, bids, asks
    FROM book
    WHERE symbol = ? AND t >= ? AND t < ?
    QUALIFY row_number() OVER (PARTITION BY t // {sample_ms} ORDER BY t DESC) = 1
),
lv AS (
    SELECT 'bid' AS side, b.price * b.qty AS usd,
           (mid - b.price) / mid * 10000 AS bps
    FROM grid, UNNEST(bids) AS _(b)
    WHERE (mid - b.price) / mid * 10000 < {max_bps}
    UNION ALL
    SELECT 'ask', a.price * a.qty,
           (a.price - mid) / mid * 10000
    FROM grid, UNNEST(asks) AS _(a)
    WHERE (a.price - mid) / mid * 10000 < {max_bps}
)
SELECT side,
       cast(floor(bps / {band_bps}) AS INTEGER) AS band,
       quantile_cont(usd, {q})                  AS p99,
       count(*)                                 AS ref_n
FROM lv
GROUP BY 1, 2
"""

_MIDS_SQL = """
SELECT t, mid FROM book
WHERE symbol = ? AND t >= ? AND t < ? {est_filter}
ORDER BY t
"""


# --------------------------------------------------------------------------
# detector
# --------------------------------------------------------------------------

class SpoofingDetector:
    """The statistical gate. No LLM, no verdicts, no network.

    Holds a :class:`features.BatchFeatureExtractor` per tracking floor rather
    than one for the whole scan, because the floor is derived per chunk from
    that chunk's own band p99 table and ``FeatureConfig`` is frozen. The
    DuckDB connection is shared, so this is cheap.
    """

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection | None = None,
        parquet_dir: Path | None = None,
        cfg: SpoofConfig | None = None,
    ) -> None:
        self.config = cfg or SpoofConfig()
        self.con = con or query.connect(parquet_dir or config.PARQUET_DIR)
        self.con.execute("SET enable_progress_bar=false")

    # -- introspection ---------------------------------------------------

    def symbols(self) -> list[str]:
        rows = self.con.execute("SELECT DISTINCT symbol FROM book ORDER BY 1").fetchall()
        return [r[0] for r in rows]

    def coverage(self, symbol: str) -> tuple[int, int] | None:
        row = self.con.execute(
            "SELECT min(t), max(t) FROM book WHERE symbol = ?", [symbol]
        ).fetchone()
        if not row or row[0] is None:
            return None
        return int(row[0]), int(row[1])

    # -- reference -------------------------------------------------------

    def band_reference(self, symbol: str, at: int) -> dict[tuple[str, int], BandRef]:
        """Trailing band p99 as of ``at``, from the ``ref_ms`` before it.

        Strictly backward-looking: the window ends at ``at``, exclusive. A
        gate whose threshold saw the event it is judging is not a gate.
        """
        cfg = self.config
        sql = _BAND_P99_SQL.format(
            sample_ms=cfg.ref_sample_ms, max_bps=cfg.max_band_bps,
            band_bps=cfg.band_bps, q=cfg.quantile,
        )
        rows = self.con.execute(sql, [symbol, at - cfg.ref_ms, at]).fetchall()
        return {
            (side, band): BandRef(side, band, float(p99 or 0.0), int(n))
            for side, band, p99, n in rows
        }

    def track_floor(self, reference: dict[tuple[str, int], BandRef]) -> float | None:
        """Cheapest size that could possibly qualify, halved.

        ``None`` when no band inside the proximity band has a usable
        reference, which means no wall judgement is possible at all in this
        chunk and the chunk is skipped rather than scanned blind.
        """
        cfg = self.config
        max_band = int(cfg.proximity_bps // cfg.band_bps)
        usable = [r.p99 for (side, band), r in reference.items()
                  if band <= max_band and r.usable and r.ref_n >= cfg.min_ref_obs]
        if not usable:
            return None
        return cfg.track_frac * cfg.wall_ratio * min(usable)

    # -- events ----------------------------------------------------------

    def events(self, symbol: str, start: int, end: int,
               min_ratio: float | None = None) -> tuple[list[SpoofEvent], GateAudit]:
        """Every episode in ``[start, end)`` that passes the per-episode gate.

        ``min_ratio`` defaults to ``config.wall_ratio``. Pass 1.0 to get the
        literal "wall > p99" reading of the frozen conjunct; the magnitude
        test is a pure post-filter on the emitted events, so the two readings
        cost one pass, not two.

        Chunked at ``chunk_ms``; the returned events are in time order across
        the whole range. Clustering is *not* done here - see :meth:`scan` -
        so that a burst spanning a chunk boundary is not lost.
        """
        cfg = self.config
        threshold = cfg.wall_ratio if min_ratio is None else min_ratio
        out: list[SpoofEvent] = []
        audit = GateAudit()
        chunk_start = (start // cfg.chunk_ms) * cfg.chunk_ms
        while chunk_start < end:
            lo = max(chunk_start, start)
            hi = min(chunk_start + cfg.chunk_ms, end)
            if lo < hi:
                evs, a = self._events_in_chunk(symbol, lo, hi)
                out.extend(evs)
                audit = audit.merged(a)
            chunk_start += cfg.chunk_ms
        out = [e for e in out if e.ratio >= threshold]
        out.sort(key=lambda e: (e.first_t, e.side, e.price))
        return out, audit

    def _events_in_chunk(
        self, symbol: str, start: int, end: int
    ) -> tuple[list[SpoofEvent], GateAudit]:
        cfg = self.config
        reference = self.band_reference(symbol, start)
        floor = self.track_floor(reference)
        if floor is None:
            return [], GateAudit(chunks=1, chunks_without_reference=1)

        fx = F.BatchFeatureExtractor(con=self.con, cfg=cfg.feature_config(floor))
        raw = fx.level_episodes(symbol, start, end)
        episodes, n_coalesced = coalesce_episodes(raw, cfg.coalesce_gap_ms)

        mids = self._mids(symbol, start, end)
        times = [t for t, _ in mids]
        values = [m for _, m in mids]

        rejects: dict[str, int] = {}
        n_plain = 0
        out: list[SpoofEvent] = []

        def reject(reason: Reject) -> None:
            rejects[reason.value] = rejects.get(reason.value, 0) + 1

        for ep in episodes:
            # --- outcome. out_of_book first: it is the one that manufactures
            # spoofing out of a trending market, so it must never be able to
            # hide behind a cheaper rejection.
            if ep.outcome is Outcome.OUT_OF_BOOK:
                reject(Reject.OUT_OF_BOOK)
                continue
            if ep.outcome is Outcome.FILLED:
                reject(Reject.FILLED)
                continue
            if ep.outcome is Outcome.PARTIAL and not cfg.include_partial:
                reject(Reject.PARTIAL)
                continue
            if ep.outcome is Outcome.OPEN:
                reject(Reject.OPEN)
                continue

            # --- distance from mid, measured over the whole episode
            bps_seen = _bps_over(ep, times, values)
            if not bps_seen:
                reject(Reject.NO_MID)
                continue
            # bps_seen[-1] is the snapshot at which the level was already
            # gone - the only one that can answer "had it left the band?" -
            # so it is deliberately not part of how close the level came
            # while it was actually resting.
            resting = bps_seen[:-1] or bps_seen
            min_bps = min(resting)
            end_bps = bps_seen[-1]
            # The level left the proximity band while still in the book -
            # price walked away from it, nobody cancelled it. Same error as
            # OUT_OF_BOOK, different mechanism.
            if end_bps > cfg.proximity_bps:
                reject(Reject.BAND_DRIFT)
                continue

            if ep.lifetime_ms >= cfg.max_lifetime_ms:
                reject(Reject.TOO_LONG)
                continue

            # --- wall. The most conservative band the level occupied, so a
            # level that dipped into a band with a low p99 cannot borrow that
            # band's easier threshold.
            bands = tuple(sorted({int(b // cfg.band_bps) for b in resting}))
            refs = [reference.get((ep.side, b)) for b in bands]
            present = [r for r in refs if r is not None and r.usable]
            if not present:
                reject(Reject.NO_REFERENCE)
                continue
            if min(r.ref_n for r in present) < cfg.min_ref_obs:
                reject(Reject.THIN_REFERENCE)
                continue
            band_p99 = max(r.p99 for r in present)
            ratio = ep.max_usd / band_p99
            if ratio <= 1.0:
                reject(Reject.BELOW_P99)
                continue
            n_plain += 1
            # Emitted regardless of magnitude; ``events()`` applies
            # ``wall_ratio`` as a post-filter so both readings of the "wall >
            # p99" conjunct come out of a single pass over the episodes.
            if ratio < cfg.wall_ratio:
                reject(Reject.BELOW_RATIO)

            out.append(SpoofEvent(
                symbol=symbol, side=ep.side, price=ep.price,
                first_t=ep.first_t, last_t=ep.last_t, max_usd=ep.max_usd,
                min_bps=min_bps, end_bps=end_bps, bands=bands,
                band_p99=band_p99, ratio=ratio,
                ref_n=min(r.ref_n for r in present),
            ))

        n_events = sum(1 for e in out if e.ratio >= cfg.wall_ratio)
        audit = GateAudit(
            n_episodes=len(episodes), n_coalesced=n_coalesced, n_events=n_events,
            n_above_plain_p99=n_plain, rejects=rejects, chunks=1,
            track_floor_usd=[floor],
        )
        return out, audit

    def _mids(self, symbol: str, start: int, end: int) -> list[tuple[int, float]]:
        """``(t, mid)`` for every snapshot the episode tracker saw.

        The same ``est`` filter as the tracker, so the two agree on which
        snapshots exist; a level's distance from mid must be measured on the
        clock its lifetime was measured on.
        """
        sql = _MIDS_SQL.format(
            est_filter="AND NOT est" if self.config.exclude_estimated else ""
        )
        return [(int(t), float(m))
                for t, m in self.con.execute(sql, [symbol, start, end]).fetchall()]

    # -- candidates ------------------------------------------------------

    def candidates(self, symbol: str, start: int, end: int) -> list[SpoofCandidate]:
        events, _ = self.events(symbol, start, end)
        return cluster_events(events, self.config.cluster_window_ms,
                              self.config.min_occurrences, self.config.cluster_by_side)

    def scan(self, symbol: str, start: int, end: int) -> ScanResult:
        """Events, candidates and the rejection funnel for one symbol."""
        cfg = self.config
        above_p99, audit = self.events(symbol, start, end, min_ratio=1.0)
        events = [e for e in above_p99 if e.ratio >= cfg.wall_ratio]
        candidates = cluster_events(events, cfg.cluster_window_ms,
                                    cfg.min_occurrences, cfg.cluster_by_side)
        scanned = audit.chunks - audit.chunks_without_reference
        return ScanResult(
            symbol=symbol, start=start, end=end,
            events=tuple(events), events_above_p99=tuple(above_p99),
            candidates=tuple(candidates), audit=audit,
            hours_scanned=scanned * cfg.chunk_ms / HOUR_MS,
        )


def _bps_over(ep: LevelEpisode, times: Sequence[int],
              values: Sequence[float]) -> list[float]:
    """Distance from mid, in bps, at every snapshot the level was resting.

    Note the half-open convention: the episode's ``last_t`` is the snapshot at
    which the level was *absent*, so it is included here precisely because the
    band-drift test needs the mid at the moment it vanished.
    """
    lo = bisect_left(times, ep.first_t)
    hi = bisect_right(times, ep.last_t)
    sign = 1.0 if ep.side == "bid" else -1.0
    return [sign * (values[i] - ep.price) / values[i] * 10_000
            for i in range(lo, hi) if values[i]]


# --------------------------------------------------------------------------
# the known wall
# --------------------------------------------------------------------------

# solusdt, 09-23 14:53:04 UTC. The largest wall the feature extractor found in
# the whole capture: $3.44M resting 3.1bps from mid at 16.9x its band p99 for
# 42 seconds. Pinned here as a regression anchor for the *finding*, not for a
# passing assertion - see the module docstring.
KNOWN_WALL = {
    "symbol": "solusdt",
    "hour": 1790172000000,     # 2026-09-23 14:00:00 UTC
    "side": "bid",
    "price": 114.58,
}


def known_wall_report(det: "SpoofingDetector") -> list[str]:
    """Walk the archive's best wall through the gate, conjunct by conjunct.

    Returns printable lines. This exists because the honest answer to "does
    the frozen design catch the thing we know is there" is *no*, and an
    answer like that should be reproducible on demand rather than living in a
    commit message.
    """
    cfg = det.config
    sym, hour = KNOWN_WALL["symbol"], KNOWN_WALL["hour"]
    lines = [f"known wall: {sym} {KNOWN_WALL['side']} @ {KNOWN_WALL['price']} "
             f"in the hour from {_iso(hour)}"]

    reference = det.band_reference(sym, hour)
    floor = det.track_floor(reference)
    if floor is None:
        lines.append("  no band reference for this hour - cannot judge")
        return lines
    lines.append(f"  tracking floor ${floor:,.0f}   "
                 + "  ".join(f"{s}{b}=${r.p99:,.0f}(n={r.ref_n:,})"
                             for (s, b), r in sorted(reference.items())
                             if b <= int(cfg.proximity_bps // cfg.band_bps)))

    fx = F.BatchFeatureExtractor(con=det.con, cfg=cfg.feature_config(floor))
    raw = fx.level_episodes(sym, hour, hour + HOUR_MS)
    mine = [e for e in raw
            if e.side == KNOWN_WALL["side"] and abs(e.price - KNOWN_WALL["price"]) < 1e-9]
    if not mine:
        lines.append("  no episode at that price in this hour")
        return lines

    stitched, merged = coalesce_episodes(mine, cfg.coalesce_gap_ms)
    lines.append(f"  {len(mine)} raw episode(s) at that price, {merged} coalesced "
                 f"-> {len(stitched)}")
    mids = det._mids(sym, hour, hour + HOUR_MS)
    times = [t for t, _ in mids]
    values = [m for _, m in mids]

    for ep in stitched:
        bps_seen = _bps_over(ep, times, values) or [float("nan")]
        bands = sorted({int(b // cfg.band_bps) for b in bps_seen})
        refs = [reference[(ep.side, b)] for b in bands if (ep.side, b) in reference]
        p99 = max((r.p99 for r in refs if r.usable), default=0.0)
        ratio = ep.max_usd / p99 if p99 else float("nan")
        lines.append(
            f"  {_iso(ep.first_t)}  ${ep.max_usd:,.0f}  {ratio:.1f}x band p99  "
            f"{min(bps_seen):.1f}bps  {ep.lifetime_ms / 1000:.1f}s  {ep.outcome.value}")
        lines.append(
            f"      wall >= {cfg.wall_ratio}x p99 : "
            f"{'PASS' if ratio >= cfg.wall_ratio else 'FAIL'}\n"
            f"      within {cfg.proximity_bps:.0f}bps    : "
            f"{'PASS' if min(bps_seen) <= cfg.proximity_bps else 'FAIL'}\n"
            f"      cancelled          : "
            f"{'PASS' if ep.outcome is Outcome.CANCELLED else 'FAIL (' + ep.outcome.value + ')'}\n"
            f"      lifetime < {cfg.max_lifetime_ms}ms   : "
            f"{'PASS' if ep.lifetime_ms < cfg.max_lifetime_ms else f'FAIL ({ep.lifetime_ms:,}ms)'}")
    return lines


# --------------------------------------------------------------------------
# diagnostic
# --------------------------------------------------------------------------

def _iso(ms: int) -> str:
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).strftime("%m-%d %H:%M:%S")


def _describe_event(ev: SpoofEvent) -> str:
    return (f"{_iso(ev.first_t)}  {ev.side} {ev.price:<10.5g} "
            f"${ev.max_usd:>12,.0f}  {ev.ratio:5.1f}x  "
            f"{ev.min_bps:5.1f}bps  {ev.lifetime_ms:>4}ms")


def _operating_points(events: Sequence[SpoofEvent], cfg: SpoofConfig,
                      hours: float) -> list[str]:
    """What the gate would emit at other thresholds.

    Printed because the frozen thresholds were chosen before anyone had
    measured this archive, and a reader is entitled to see how sharply the
    answer depends on them without having to re-run anything.
    """
    lines = []
    for ratio in (1.0, 2.0, 3.0, 5.0, 10.0):
        kept = [e for e in events if e.ratio >= ratio]
        row = f"    ratio >= {ratio:>4.1f}x : {len(kept):>6,} events"
        if hours:
            row += f" ({len(kept) / hours:6.2f}/h)"
        for k in (2, 3, 4):
            cands = cluster_events(kept, cfg.cluster_window_ms, k, cfg.cluster_by_side)
            row += f"   >={k}/90s: {len(cands):>4,}"
        lines.append(row)
    return lines


def main(argv: list[str]) -> int:
    parquet_dir = Path(os.environ["MARKETGUARD_PARQUET"]) if os.environ.get(
        "MARKETGUARD_PARQUET") else config.PARQUET_DIR
    if not parquet_dir.exists():
        print(f"no parquet at {parquet_dir} - run: python etl.py")
        return 1

    cfg = SpoofConfig()
    det = SpoofingDetector(parquet_dir=parquet_dir, cfg=cfg)
    print(config.describe())
    print(f"parquet: {parquet_dir}")
    print(f"gate: wall >= {cfg.wall_ratio}x band p99  AND  within "
          f"{cfg.proximity_bps:.0f}bps  AND  cancelled < {cfg.max_lifetime_ms}ms  "
          f"AND  >= {cfg.min_occurrences} in {cfg.cluster_window_ms // 1000}s\n")

    if "--known-wall" in argv:
        for line in known_wall_report(det):
            print(line)
        return 0

    wanted = [a for a in argv if not a.startswith("-")]
    symbols = [wanted[0]] if wanted else det.symbols()
    hours = int(wanted[1]) if len(wanted) > 1 else 24
    for arg in argv:
        if arg.startswith("--hours="):
            hours = int(arg.split("=", 1)[1])

    all_events: list[SpoofEvent] = []
    total_hours = 0.0
    total_candidates = 0

    for symbol in symbols:
        cov = det.coverage(symbol)
        if cov is None:
            print(f"{symbol}: no data")
            continue
        first, last = cov
        end = last + 1
        # The reference is strictly trailing, so the first ref_ms of capture
        # can never be judged. Starting there would report a quiet opening
        # that is really an absence of a threshold.
        start = max(first + cfg.ref_ms, end - hours * HOUR_MS)
        if start >= end:
            print(f"{symbol}: less than {cfg.ref_ms // HOUR_MS}h of history; "
                  "no trailing reference")
            continue

        result = det.scan(symbol, start, end)
        a = result.audit
        print(f"=== {symbol}  {_iso(start)} .. {_iso(end)}  "
              f"{result.hours_scanned:.0f}h scanned "
              f"({a.chunks_without_reference} chunk(s) without a reference) ===")
        print(f"  funnel: {a.funnel()}")
        if a.track_floor_usd:
            print(f"  tracking floor: median ${statistics.median(a.track_floor_usd):,.0f}"
                  f"  (min ${min(a.track_floor_usd):,.0f}, "
                  f"max ${max(a.track_floor_usd):,.0f})")
        print(f"  events {len(result.events):,} ({result.events_per_hour:.2f}/h)   "
              f"candidates {len(result.candidates):,} "
              f"({result.candidates_per_hour:.3f}/h)")

        for ev in sorted(result.events, key=lambda e: -e.ratio)[:5]:
            print(f"    top event  {_describe_event(ev)}")
        for c in sorted(result.candidates, key=lambda c: -c.max_ratio)[:5]:
            print(f"    CANDIDATE  {_iso(c.start_t)}  {c.side}  "
                  f"{c.n_events} events / {c.span_ms / 1000:.1f}s  "
                  f"{c.n_distinct_prices} price(s)  "
                  f"max ${c.max_usd:,.0f} at {c.max_ratio:.1f}x  "
                  f"{c.min_bps:.1f}bps  median life {c.median_lifetime_ms:.0f}ms")

        print("  operating points (what other thresholds would have said):")
        for line in _operating_points(result.events_above_p99, cfg,
                                      result.hours_scanned):
            print(line)
        print()

        all_events.extend(result.events)
        total_hours += result.hours_scanned
        total_candidates += len(result.candidates)

    if len(symbols) > 1:
        print(f"=== all symbols: {len(all_events):,} events, "
              f"{total_candidates:,} candidates over {total_hours:.0f} symbol-hours "
              f"({total_candidates / total_hours if total_hours else 0:.4f} "
              f"candidates per symbol-hour) ===")

    print()
    for line in known_wall_report(det):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
