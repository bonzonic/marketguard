"""Tests for the spoofing gate.

Every fixture here is synthetic, for the same reasons test_features.py gives:
the recorded archive is irreplaceable, a live recorder is appending to it, and
its contents change under you. Running ``spoofing.py`` against real capture is
a plausibility check, not a test.

The emphasis is on the ways this gate could be wrong while still producing
plausible output:

    * each conjunct silently not binding, so the gate is a disjunction
      wearing a conjunction's docstring;
    * ``out_of_book`` counted as a cancellation, which manufactures spoofing
      out of any trending market;
    * a level that drifted out of the proximity band counted as a
      cancellation - the same error by a different route, and the one
      features.py cannot see;
    * size compared across two different prices, which is the
      ``best_bid = prev_bid`` trap;
    * one wall that wobbled counted as three placements, which is the only
      way a single event can promote itself into a candidate.

The low-level book helpers come from test_features rather than being copied:
they are the only place that knows how to load rows through Arrow into tables
shaped like the parquet views, and two copies of that would drift apart.
"""
from __future__ import annotations

import pytest

import features as F
import spoofing as S
from features import LevelEpisode, Outcome
from test_features import add_snapshot, add_trade, flush, make_con

HOUR = S.HOUR_MS
STEP = 100          # the @depth20@100ms cadence
# Hour-aligned on purpose: the detector chunks on absolute hour boundaries,
# so an unaligned origin silently splits a one-hour fixture across two chunks
# and gives the second one a reference window that overlaps the first one's
# detection data.
T0 = 1_700_000_000_000 // HOUR * HOUR


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def test_config_rejects_a_wall_ratio_weaker_than_p99_itself():
    """``wall_ratio < 1`` makes the magnitude conjunct weaker than the bare
    p99 exceedance it exists to replace, which silently disables the only
    conjunct that discriminates."""
    with pytest.raises(ValueError):
        S.SpoofConfig(wall_ratio=0.5)


def test_config_defaults_track_the_feature_extractor():
    """The band p99 here and the band p99 in features.py must be the same
    estimator, or a ``ratio`` reported by one means nothing to the other."""
    cfg, fcfg = S.SpoofConfig(), F.FeatureConfig()
    assert cfg.wall_ratio == fcfg.wall_flag_ratio
    assert cfg.band_bps == fcfg.wall_band_bps
    assert cfg.max_band_bps == fcfg.wall_max_bps
    assert cfg.ref_sample_ms == fcfg.wall_sample_ms
    assert cfg.quantile == fcfg.wall_quantile
    assert cfg.ref_ms == fcfg.wall_ref_ms
    assert cfg.min_ref_obs == fcfg.wall_min_ref_obs


def test_feature_config_pins_the_proximity_band():
    """The "within 20bps" conjunct is enforced by the episode tracker, so the
    two numbers must not be settable independently."""
    fcfg = S.SpoofConfig(proximity_bps=12.0).feature_config(level_min_usd=1_000.0)
    assert fcfg.level_max_bps == 12.0
    assert fcfg.wall_proximity_bps == 12.0
    assert fcfg.level_min_usd == 1_000.0


def test_config_validates():
    with pytest.raises(ValueError):
        S.SpoofConfig(min_occurrences=0)
    with pytest.raises(ValueError):
        S.SpoofConfig(track_frac=0.0)
    with pytest.raises(ValueError):
        S.SpoofConfig(max_lifetime_ms=0)


# --------------------------------------------------------------------------
# clustering - pure
# --------------------------------------------------------------------------

def ev(t, side="bid", price=100.0, ratio=5.0, symbol="TEST", life=100):
    return S.SpoofEvent(
        symbol=symbol, side=side, price=price, first_t=t, last_t=t + life,
        max_usd=1_000_000.0, min_bps=3.0, end_bps=3.0, bands=(0,),
        band_p99=200_000.0, ratio=ratio, ref_n=10_000,
    )


def test_three_in_ninety_seconds_is_a_candidate():
    cands = S.cluster_events([ev(T0), ev(T0 + 30_000), ev(T0 + 60_000)], 90_000, 3)
    assert len(cands) == 1
    assert cands[0].n_events == 3
    assert cands[0].span_ms == 60_100


def test_two_in_ninety_seconds_is_not():
    assert S.cluster_events([ev(T0), ev(T0 + 30_000)], 90_000, 3) == []


def test_three_spread_over_more_than_ninety_seconds_is_not():
    """The window is the point: three events an hour apart are three ordinary
    large orders, not a burst."""
    assert S.cluster_events(
        [ev(T0), ev(T0 + 60_000), ev(T0 + 120_000)], 90_000, 3) == []


def test_window_boundary_is_inclusive():
    assert len(S.cluster_events(
        [ev(T0), ev(T0 + 45_000), ev(T0 + 90_000)], 90_000, 3)) == 1
    assert S.cluster_events(
        [ev(T0), ev(T0 + 45_000), ev(T0 + 90_001)], 90_000, 3) == []


def test_overlapping_bursts_are_reported_once():
    """Five events inside the window qualify at three different window
    positions. Emitting a candidate for each announces the same burst three
    times, which is the crying-wolf failure this project ranks first."""
    cands = S.cluster_events([ev(T0 + i * 10_000) for i in range(5)], 90_000, 3)
    assert len(cands) == 1
    assert cands[0].n_events == 5


def test_separate_bursts_stay_separate():
    events = ([ev(T0 + i * 10_000) for i in range(3)]
              + [ev(T0 + 600_000 + i * 10_000) for i in range(3)])
    assert len(S.cluster_events(events, 90_000, 3)) == 2


def test_clustering_by_side_refuses_to_mix_sides():
    """A quote-stuffer flickering both sides through a volatility burst is
    not the one-sided pressure a spoofer applies."""
    events = [ev(T0, side="bid"), ev(T0 + 1_000, side="ask"),
              ev(T0 + 2_000, side="bid")]
    assert S.cluster_events(events, 90_000, 3, by_side=True) == []
    mixed = S.cluster_events(events, 90_000, 3, by_side=False)
    assert len(mixed) == 1 and mixed[0].side is None


def test_clustering_never_mixes_symbols():
    events = [ev(T0, symbol="AAA"), ev(T0 + 1_000, symbol="BBB"),
              ev(T0 + 2_000, symbol="AAA")]
    assert S.cluster_events(events, 90_000, 3, by_side=False) == []


def test_candidate_counts_distinct_prices():
    """One price across a whole burst is either the textbook place-and-pull
    pattern or an un-stitched single wall, and a reviewer needs to see
    which."""
    c = S.cluster_events([ev(T0, price=100.0), ev(T0 + 1_000, price=100.0),
                          ev(T0 + 2_000, price=99.99)], 90_000, 3)[0]
    assert c.n_distinct_prices == 2
    assert c.summary()["n_distinct_prices"] == 2


def test_cluster_rejects_a_nonsense_threshold():
    with pytest.raises(ValueError):
        S.cluster_events([], 90_000, 0)


# --------------------------------------------------------------------------
# coalescing - pure
# --------------------------------------------------------------------------

def episode(first_t, last_t, price=100.0, side="bid", usd=1_000_000.0,
            outcome=Outcome.CANCELLED, filled=0.0):
    return LevelEpisode(
        symbol="TEST", side=side, price=price, first_t=first_t, last_t=last_t,
        max_usd=usd, last_usd=usd, filled_qty=filled, outcome=outcome,
    )


def test_fragments_of_one_wall_become_one_event():
    """A wall wobbling across the tracking floor re-opens its episode. Left
    alone, one placement supplies every occurrence the repetition conjunct
    needs."""
    eps = [episode(T0, T0 + 5_000), episode(T0 + 5_200, T0 + 9_000),
           episode(T0 + 9_100, T0 + 12_000)]
    out, merged = S.coalesce_episodes(eps, 300)
    assert merged == 2
    assert len(out) == 1
    assert (out[0].first_t, out[0].last_t) == (T0, T0 + 12_000)


def test_a_real_gap_is_not_coalesced():
    out, merged = S.coalesce_episodes(
        [episode(T0, T0 + 5_000), episode(T0 + 20_000, T0 + 22_000)], 300)
    assert merged == 0 and len(out) == 2


def test_coalescing_never_crosses_a_price():
    """Stitching two prices together is the depth-comparison trap in another
    form."""
    out, merged = S.coalesce_episodes(
        [episode(T0, T0 + 1_000, price=100.0),
         episode(T0 + 1_100, T0 + 2_000, price=99.99)], 300)
    assert merged == 0 and len(out) == 2


def test_coalescing_never_crosses_a_side():
    out, merged = S.coalesce_episodes(
        [episode(T0, T0 + 1_000, side="bid"),
         episode(T0 + 1_100, T0 + 2_000, side="ask")], 300)
    assert merged == 0 and len(out) == 2


def test_merged_episode_keeps_the_last_outcome_and_the_largest_size():
    """How the level finally left is the only departure that describes the
    whole placement."""
    out, _ = S.coalesce_episodes(
        [episode(T0, T0 + 1_000, usd=500_000.0, outcome=Outcome.CANCELLED),
         episode(T0 + 1_100, T0 + 2_000, usd=900_000.0,
                 outcome=Outcome.FILLED, filled=7.0)], 300)
    assert out[0].max_usd == 900_000.0
    assert out[0].outcome is Outcome.FILLED
    assert out[0].filled_qty == 7.0


def test_coalescing_can_be_switched_off():
    out, merged = S.coalesce_episodes(
        [episode(T0, T0 + 1_000), episode(T0 + 1_100, T0 + 2_000)], 0)
    assert merged == 0 and len(out) == 2


# --------------------------------------------------------------------------
# synthetic book
#
# A $100 book on a 1-cent tick: level i is almost exactly (i+1) bps from mid,
# so "20bps" is "20 levels" and a 20-level feed spans exactly the proximity
# band. Reference sizes cycle over 100 values so the band p99 is a real
# percentile with real headroom above it - a flat reference would put p99 and
# the median at the same number and every threshold test would pass for the
# wrong reason.
# --------------------------------------------------------------------------

TICK = 0.01
BASE = 100.0
ORDINARY = 10.0                 # ~$1,000 a level
REF_STEP = 5_000                # reference snapshot spacing
# What the fixtures below are working against, once quiet_history has run:
#   band p99      ~ $10,800   (reference sizes cycle 10.0 .. 109.0)
#   tracking floor~ $16,200   (= track_frac * wall_ratio * min band p99)
#   qualifying    ~ $32,400   (= wall_ratio * band p99)


def book_at(mid, wall=None, n=20, qty=ORDINARY, ask_qty=None):
    """``(bids, asks)`` around ``mid``; ``wall`` is ``(side, price, qty)``."""
    bids = [[round(mid - TICK * (i + 1), 4), qty] for i in range(n)]
    asks = [[round(mid + TICK * (i + 1), 4),
             qty if ask_qty is None else ask_qty] for i in range(n)]
    if wall:
        side, price, wqty = wall
        rows = bids if side == "bid" else asks
        for row in rows:
            if abs(row[0] - price) < 1e-9:
                row[1] = wqty
                break
        else:                                   # not one of the visible levels
            rows.append([price, wqty])
            rows.sort(key=lambda r: -r[0] if side == "bid" else r[0])
    return ([tuple(r) for r in bids], [tuple(r) for r in asks])


def quiet_history(con, t_from, t_to, step=REF_STEP, mid=BASE):
    """Ordinary book, sizes cycling 10.0 .. 109.0, to build the reference.

    A flat reference would put the median and the p99 at the same number and
    every magnitude test below would pass for the wrong reason.
    """
    for k, t in enumerate(range(t_from, t_to, step)):
        bids, asks = book_at(mid, qty=ORDINARY + (k % 100) * ORDINARY / 10)
        add_snapshot(con, t, bids, asks)


def spoof_config(**overrides):
    base = dict(
        wall_ratio=3.0, proximity_bps=20.0, max_lifetime_ms=500,
        min_occurrences=3, cluster_window_ms=90_000, coalesce_gap_ms=300,
        # Production wants 2,000 observations behind the p99. A fixture
        # writing 2,000 rows per band would be a slow test for no extra
        # coverage; the gate itself is covered by
        # test_thin_reference_is_reported_not_ignored.
        min_ref_obs=200, ref_ms=HOUR, chunk_ms=HOUR,
    )
    base.update(overrides)
    return S.SpoofConfig(**base)


def detector(con, **overrides):
    flush(con)
    return S.SpoofingDetector(con=con, cfg=spoof_config(**overrides))


def detection_hour(con, snapshots, hour=None, reference=True):
    """Build a reference hour then a detection hour from ``snapshots``.

    ``snapshots`` is an iterable of ``(dt, bids, asks, est)``.
    """
    hour = hour if hour is not None else T0 + HOUR
    if reference:
        quiet_history(con, hour - HOUR, hour)
    for dt, bids, asks, est in snapshots:
        add_snapshot(con, hour + dt, bids, asks, est=est)
    return hour


def wall_run(wall_qty, present_ms, price=99.99, side="bid", span=60_000,
             start=200, mid=BASE, n=20, est=False):
    """Snapshots for one wall placed ``start`` ms in and pulled cleanly."""
    for dt in range(0, span, STEP):
        wall = (side, price, wall_qty) if start <= dt < start + present_ms else None
        bids, asks = book_at(mid, wall, n=n)
        yield dt, bids, asks, est and wall is not None


def one_wall(**kwargs):
    con = make_con()
    return con, detection_hour(con, wall_run(**kwargs))


def events_of(con, hour, **overrides):
    return detector(con, **overrides).events("TEST", hour, hour + HOUR)


# --------------------------------------------------------------------------
# the reference
# --------------------------------------------------------------------------

def test_band_reference_is_strictly_backward_looking():
    """A threshold that saw the event it is judging is not a threshold.

    The detection hour here is wall-to-wall enormous levels; the reference
    taken at the hour boundary must be innocent of every one of them.
    """
    con = make_con()
    hour = detection_hour(con, wall_run(100_000.0, 60_000, start=0))
    ref = detector(con).band_reference("TEST", hour)
    assert ref
    assert all(r.p99 < 200 * BASE for r in ref.values())   # qty < 200


def test_track_floor_is_derived_from_the_reference():
    """features.level_min_usd is absolute and therefore either unaffordable
    or blind depending on the symbol; this floor is the cheapest size that
    could possibly qualify, halved."""
    con = make_con()
    quiet_history(con, T0, T0 + HOUR)
    det = detector(con)
    ref = det.band_reference("TEST", T0 + HOUR)
    floor = det.track_floor(ref)
    cfg = det.config
    near = [r.p99 for (_, band), r in ref.items()
            if band <= int(cfg.proximity_bps // cfg.band_bps)]
    assert floor == pytest.approx(cfg.track_frac * cfg.wall_ratio * min(near))


def test_no_reference_means_the_chunk_is_skipped_not_scanned_blind():
    """With no trailing history there is no answer to "is this large", and
    reporting zero events is a different statement from reporting that."""
    con = make_con()
    hour = detection_hour(con, wall_run(100_000.0, 300), hour=T0,
                          reference=False)
    events, audit = detector(con).events("TEST", hour, hour + HOUR)
    assert events == []
    assert audit.chunks_without_reference == 1
    assert audit.n_episodes == 0


def test_thin_reference_is_reported_not_ignored():
    con, hour = one_wall(wall_qty=6_000.0, present_ms=300)
    events, audit = events_of(con, hour, min_ref_obs=10_000_000)
    assert events == []
    assert audit.chunks_without_reference == 1


# --------------------------------------------------------------------------
# each conjunct, failing in isolation
# --------------------------------------------------------------------------

def test_the_full_conjunction_passes_on_a_clean_spoof():
    """The positive control. Every test below removes exactly one conjunct
    from this fixture."""
    con, hour = one_wall(wall_qty=6_000.0, present_ms=300)
    events, audit = events_of(con, hour)
    assert len(events) == 1
    e = events[0]
    assert (e.side, e.price, e.lifetime_ms) == ("bid", 99.99, 300)
    assert e.ratio >= 3.0
    assert e.min_bps <= 20.0
    assert e.above_plain_p99
    assert audit.rejects.get("out_of_book", 0) == 0


def test_wall_conjunct_an_ordinary_order_is_not_even_tracked():
    """The tracking floor is derived from the qualifying threshold, so
    ordinary size costs nothing to ignore."""
    con, hour = one_wall(wall_qty=ORDINARY * 1.5, present_ms=300)
    events, audit = events_of(con, hour)
    assert events == []
    assert audit.n_episodes == 0


def test_wall_conjunct_plain_p99_is_much_weaker_than_the_ratio():
    """Trap 1 in miniature. An order that clears p99 but not 3x p99 is an
    event under the literal reading of the frozen conjunct and rejected under
    the one this module uses - and on real data that gap is two orders of
    magnitude."""
    con, hour = one_wall(wall_qty=250.0, present_ms=300)   # ~$25k, ~2.3x p99
    det = detector(con)
    strict, audit = det.events("TEST", hour, hour + HOUR)
    loose, _ = det.events("TEST", hour, hour + HOUR, min_ratio=1.0)
    assert strict == []
    assert len(loose) == 1
    assert 1.0 < loose[0].ratio < 3.0
    assert audit.rejects["below_ratio"] == 1
    assert audit.n_above_plain_p99 == 1


def test_proximity_conjunct_a_wall_outside_the_band_is_not_tracked():
    """A wall 100bps out persuades nobody, and the tracker never sees it."""
    con, hour = one_wall(wall_qty=6_000.0, present_ms=300, price=99.00, n=200)
    events, audit = events_of(con, hour)
    assert events == []
    assert audit.n_episodes == 0


def test_lifetime_conjunct_a_slow_wall_is_too_long():
    con, hour = one_wall(wall_qty=6_000.0, present_ms=2_000)
    events, audit = events_of(con, hour)
    assert events == []
    assert audit.rejects["too_long"] == 1


def test_lifetime_conjunct_binds_at_the_configured_edge():
    con, hour = one_wall(wall_qty=6_000.0, present_ms=500)
    events, audit = events_of(con, hour)
    assert events == []                        # 500ms is not < 500ms
    assert audit.rejects["too_long"] == 1

    con, hour = one_wall(wall_qty=6_000.0, present_ms=400)
    events, _ = events_of(con, hour)
    assert len(events) == 1 and events[0].lifetime_ms == 400


def test_repetition_conjunct_one_event_is_not_a_candidate():
    con, hour = one_wall(wall_qty=6_000.0, present_ms=300)
    result = detector(con).scan("TEST", hour, hour + HOUR)
    assert len(result.events) == 1
    assert result.candidates == ()


def test_repetition_conjunct_three_placements_are_a_candidate():
    """Three placements at three prices, 20s apart, each pulled in 300ms."""
    placements = [(20_000, 99.99), (40_000, 99.98), (60_000, 99.97)]

    def snapshots():
        for dt in range(0, 120_000, STEP):
            wall = next((("bid", p, 6_000.0) for at, p in placements
                         if at <= dt < at + 300), None)
            bids, asks = book_at(BASE, wall)
            yield dt, bids, asks, False

    con = make_con()
    hour = detection_hour(con, snapshots())
    result = detector(con).scan("TEST", hour, hour + HOUR)
    assert len(result.events) == 3
    assert len(result.candidates) == 1
    c = result.candidates[0]
    assert (c.n_events, c.side, c.n_distinct_prices) == (3, "bid", 3)
    assert c.median_lifetime_ms == 300
    assert c.summary()["symbol"] == "TEST"


# --------------------------------------------------------------------------
# out_of_book - trap 2
# --------------------------------------------------------------------------

def test_a_level_that_falls_out_of_the_window_is_not_a_cancellation():
    """Price runs away from a resting bid until it drops off the bottom of
    the twenty-level feed. Nobody cancelled anything. Scoring this as a
    cancel is how a trending market gets reported as spoofed all the way
    up."""
    def snapshots():
        for dt in range(0, 60_000, STEP):
            if dt < 400:
                bids, asks = book_at(BASE, ("bid", 99.99, 6_000.0))
            else:
                bids, asks = book_at(BASE + 1.0)   # 99.99 is below the floor
            yield dt, bids, asks, False

    con = make_con()
    hour = detection_hour(con, snapshots())
    events, audit = events_of(con, hour)
    assert events == []
    assert audit.rejects["out_of_book"] == 1
    assert audit.rejects.get("too_long", 0) == 0


def test_a_trending_market_produces_no_events_at_all():
    """The end-to-end form of the same trap: a book full of large levels
    walking steadily upward, with nobody cancelling anything. Every level it
    leaves behind departs ``out_of_book``.

    The book is 15 levels deep against a 20bps proximity band, so levels fall
    out of the *window* before they drift out of the band - which isolates
    this from the band-drift test below.
    """
    def snapshots():
        for k, dt in enumerate(range(0, 120_000, STEP)):
            # Large bids only. Ordinary asks stay under the tracking floor,
            # which keeps the fixture about the bid side being left behind
            # rather than about ask levels the rising mid crosses through.
            bids, asks = book_at(round(BASE + k * TICK, 4), qty=6_000.0,
                                 ask_qty=ORDINARY, n=15)
            yield dt, bids, asks, False

    con = make_con()
    hour = detection_hour(con, snapshots())
    result = detector(con).scan("TEST", hour, hour + HOUR)
    assert result.audit.n_episodes > 100          # plenty of departures
    assert result.audit.rejects["out_of_book"] > 100
    assert result.events == ()
    assert result.candidates == ()


def test_a_filled_level_is_not_a_cancellation():
    con = make_con()
    hour = detection_hour(con, wall_run(6_000.0, 300))
    add_trade(con, hour + 450, 99.99, 6_000.0, aggressive_buy=False)
    events, audit = events_of(con, hour)
    assert events == []
    assert audit.rejects["filled"] == 1


def test_a_partially_filled_level_is_excluded_by_default_and_can_be_included():
    """Excluding PARTIAL is a deliberate precision-over-recall choice, so it
    has to be a choice: visible in the audit and reversible by config."""
    con = make_con()
    hour = detection_hour(con, wall_run(6_000.0, 300))
    add_trade(con, hour + 450, 99.99, 10.0, aggressive_buy=False)
    events, audit = events_of(con, hour)
    assert events == []
    assert audit.rejects["partial"] == 1
    included, _ = events_of(con, hour, include_partial=True)
    assert len(included) == 1


# --------------------------------------------------------------------------
# band drift - the fourth departure
# --------------------------------------------------------------------------

def test_a_level_that_drifts_out_of_the_proximity_band_is_not_a_cancellation():
    """Mid rises until a resting bid is further than ``proximity_bps`` away.
    The level is still in the book, still at its price, and nobody touched
    it - but it leaves the tracked set, which looks exactly like a cancel.

    The book is 60 levels deep so the level never leaves the *window*, which
    isolates band drift from ``out_of_book``.
    """
    def snapshots():
        for dt in range(0, 60_000, STEP):
            mid = BASE if dt < 300 else BASE + 0.30      # 1bps -> 31bps away
            bids, asks = book_at(mid, ("bid", 99.99, 6_000.0), n=60)
            yield dt, bids, asks, False

    con = make_con()
    hour = detection_hour(con, snapshots())
    events, audit = events_of(con, hour)
    assert events == []
    assert audit.rejects["band_drift"] == 1
    assert audit.rejects.get("out_of_book", 0) == 0


def test_a_level_that_drifts_closer_to_mid_still_qualifies():
    """Drift only disqualifies outward. A wall the market walks *toward* is
    more prominent, not less."""
    def snapshots():
        for dt in range(0, 60_000, STEP):
            mid = BASE + 0.10 if dt < 200 else BASE
            wall = ("bid", 99.99, 6_000.0) if 100 <= dt < 400 else None
            bids, asks = book_at(mid, wall, n=60)
            yield dt, bids, asks, False

    con = make_con()
    hour = detection_hour(con, snapshots())
    events, audit = events_of(con, hour)
    assert audit.rejects.get("band_drift", 0) == 0
    assert len(events) == 1
    assert events[0].min_bps < 2.0               # ended up right against mid
    assert events[0].end_bps < 2.0


# --------------------------------------------------------------------------
# best_bid = prev_bid - trap 3
# --------------------------------------------------------------------------

def test_top_of_book_flicker_between_ticks_is_not_a_wall():
    """The classic artifact: the best bid alternates between two adjacent
    ticks, one carrying a dust size and one carrying an ordinary one. A
    ``lag(best_bid_qty)`` comparison reports a 10,000x multiplier every
    100ms. This gate keys every episode on price, so there is nothing to
    compare and nothing to report.
    """
    def snapshots():
        for k, dt in enumerate(range(0, 120_000, STEP)):
            bids, asks = book_at(BASE)
            if k % 2:
                bids = [(99.995, 0.001)] + list(bids[:19])
            yield dt, bids, asks, False

    con = make_con()
    hour = detection_hour(con, snapshots())
    det = detector(con)

    # The artifact is real: confirm the naive comparison really does produce
    # a dramatic-looking multiplier on this fixture.
    naive = det.con.execute("""
        SELECT max(best_bid_qty / nullif(prev, 0)) FROM (
            SELECT best_bid_qty, lag(best_bid_qty) OVER (ORDER BY t) AS prev
            FROM book WHERE t >= ?)
    """, [hour]).fetchone()[0]
    assert naive > 1_000

    result = det.scan("TEST", hour, hour + HOUR)
    assert result.audit.n_episodes == 0
    assert result.events == ()
    assert result.candidates == ()


def test_size_is_never_compared_across_two_prices():
    """A wall that reappears one tick down is two placements, not one order
    that grew fiftyfold. Coalescing keys on price; this pins that."""
    out, merged = S.coalesce_episodes(
        [episode(T0, T0 + 200, price=100.0, usd=100_000.0),
         episode(T0 + 300, T0 + 500, price=99.99, usd=5_000_000.0)], 300)
    assert merged == 0
    assert {e.max_usd for e in out} == {100_000.0, 5_000_000.0}


# --------------------------------------------------------------------------
# fragmentation - trap 4
# --------------------------------------------------------------------------

def test_one_wobbling_wall_is_not_three_placements():
    """The wall dips under the tracking floor twice, for one snapshot each.
    Un-stitched that is three cancels at one price inside 90s, which is a
    candidate. Stitched it is one placement that rested far too long.
    """
    def snapshots():
        for dt in range(0, 60_000, STEP):
            if dt < 200 or dt >= 1_400:
                wall = None
            elif dt in (600, 1_000):
                wall = ("bid", 99.99, 20.0)      # momentarily under the floor
            else:
                wall = ("bid", 99.99, 6_000.0)
            bids, asks = book_at(BASE, wall)
            yield dt, bids, asks, False

    con = make_con()
    hour = detection_hour(con, snapshots())

    det = detector(con)
    stitched = det.scan("TEST", hour, hour + HOUR)
    assert stitched.audit.n_coalesced == 2
    assert stitched.audit.rejects["too_long"] == 1
    assert stitched.events == ()
    assert stitched.candidates == ()

    loose = S.SpoofingDetector(con=det.con, cfg=spoof_config(coalesce_gap_ms=0))
    artifact = loose.scan("TEST", hour, hour + HOUR)
    assert artifact.audit.n_coalesced == 0
    assert len(artifact.candidates) == 1          # exactly what the guard stops
    assert artifact.candidates[0].n_distinct_prices == 1


# --------------------------------------------------------------------------
# reconstructed timestamps
# --------------------------------------------------------------------------

def test_reconstructed_timestamps_are_excluded_from_the_gate():
    """est:1 stamps are ~67ms off at p90 against a 500ms conjunct - a 13%
    error on the quantity the gate turns on. A spoof that exists only in
    reconstructed snapshots must vanish rather than be timed badly."""
    con = make_con()
    hour = detection_hour(con, wall_run(6_000.0, 300, est=True))
    events, _ = events_of(con, hour)
    assert events == []
    included, _ = events_of(con, hour, exclude_estimated=False)
    assert len(included) == 1


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def test_scan_reports_both_readings_of_the_wall_conjunct():
    con, hour = one_wall(wall_qty=250.0, present_ms=300)   # ~$25k, ~2.3x p99
    result = detector(con).scan("TEST", hour, hour + HOUR)
    assert result.events == ()
    assert len(result.events_above_p99) == 1
    assert result.events_per_hour == 0.0
    assert result.hours_scanned == 1.0


def test_audit_funnel_is_printable_and_accounts_for_every_episode():
    con, hour = one_wall(wall_qty=6_000.0, present_ms=2_000)
    _, audit = events_of(con, hour)
    assert "too_long" in audit.funnel()
    assert audit.n_episodes == sum(audit.rejects.values()) + audit.n_events


def test_quiet_market_produces_nothing():
    con = make_con()
    quiet_history(con, T0, T0 + 2 * HOUR)
    result = detector(con).scan("TEST", T0 + HOUR, T0 + 2 * HOUR)
    assert result.events == ()
    assert result.candidates == ()


def test_empty_range_is_empty_not_an_error():
    con = make_con()
    quiet_history(con, T0, T0 + HOUR)
    events, _ = detector(con).events("TEST", T0 + HOUR, T0 + HOUR)
    assert events == []


def test_known_wall_constant_points_at_an_hour_boundary():
    """A cheap guard on the anchor: if someone edits KNOWN_WALL the reported
    finding quietly stops being about the wall it names."""
    assert S.KNOWN_WALL["symbol"] == "solusdt"
    assert S.KNOWN_WALL["side"] == "bid"
    assert S.KNOWN_WALL["hour"] % HOUR == 0
