"""Tests for the pump & dump gate.

**The archive contains no pump and dump**, so there is no positive example to
validate against and nothing here touches recorded data. Every fixture is a
synthetic market with a known shape, and the assertions are about the gate's
*behaviour on each shape* rather than about a number it happens to produce:

    clean pump and dump        candidate, and the retracement confirms
    pump that never dumps      candidate, and confirmation returns None
    slow organic rally         same total move, spread out - no candidate
    volume spike, flat price   no candidate
    dump with no pump before   no candidate (and no confirmation)
    deep book during the pump  no candidate - the thin-book leg is a target
    two-sided flow             no candidate - OFI leg fails
    sell-side pump             no candidate - the spike sign is kept

The second emphasis is the boundary this module exists to keep: a
:class:`~pumpdump.Candidate` must never be obtainable as a confirmed finding.
Those tests assert on types and exceptions, not on thresholds, because that
boundary is a structural property and a threshold is not.

Running pumpdump.py against the real archive is a plausibility check and a
source of distributions for a later tuning pass. It is not a test, and none
of these tests depend on it.
"""
from __future__ import annotations

import dataclasses
import random

import pytest

import features as F
import pumpdump as PD
from test_features import (
    MINUTE,
    T0,
    add_snapshot,
    add_trade,
    build,
    make_con,
)


# --------------------------------------------------------------------------
# synthetic market construction
# --------------------------------------------------------------------------

SNAPSHOTS_PER_BAR = 4


def book_at(con, bar_start, price, depth_qty, symbol="TEST", n=8, tick=0.01,
            snapshots=SNAPSHOTS_PER_BAR, drift_to=None):
    """One bar's worth of book snapshots around ``price``.

    ``drift_to`` walks mid linearly across the bar, which is what gives the
    bar a high and a low the retracement can see. ``depth_qty`` is the size
    resting on every level, so shrinking it thins the book without moving
    price - the two effects have to be separable or the thin-book leg cannot
    be tested on its own.
    """
    step = MINUTE // snapshots
    end = price if drift_to is None else drift_to
    for k in range(snapshots):
        mid = price + (end - price) * (k / max(1, snapshots - 1))
        bids = [(round(mid - tick * (i + 1), 6), depth_qty) for i in range(n)]
        asks = [(round(mid + tick * (i + 1), 6), depth_qty) for i in range(n)]
        add_snapshot(con, bar_start + k * step, bids, asks, symbol)


def trades_at(con, bar_start, price, buy_usd, sell_usd, symbol="TEST"):
    """Two prints per bar carrying a known notional on each side.

    Split across several prints so a bar is never a single trade: ``m`` is
    resolved per trade, and a one-print bar would let a sign error hide.
    """
    for i in range(2):
        if buy_usd > 0:
            add_trade(con, bar_start + 1_000 + i * 100, price,
                      buy_usd / 2 / price, True, symbol)
        if sell_usd > 0:
            add_trade(con, bar_start + 2_000 + i * 100, price,
                      sell_usd / 2 / price, False, symbol)


# A fixed, deterministic jitter sequence. Volume and price both need a
# non-degenerate baseline: a perfectly regular fixture gives MAD 0 (robust_z
# reports ZERO_SCALE rather than a number) or a near-zero MAD that turns
# every z into a meaningless eight-figure value. Either way the thresholds
# under test stop being exercised. Seeded, so the suite is reproducible.
_RNG = random.Random(20260927)
_PRICE_JITTER = [_RNG.gauss(0.0, 0.0008) for _ in range(4_000)]
_VOLUME_JITTER = [_RNG.uniform(0.6, 1.6) for _ in range(4_000)]


def quiet_bar(con, i, price=100.0, depth_qty=100.0, symbol="TEST"):
    """A placid minute: modest balanced volume, deep book, small price noise.

    The noise is what gives the symbol its *own* scale - 8bps of typical
    one-minute movement here - so the vol-normalised spike has something to
    normalise against and the 5-MAD cut point means something.
    """
    bar = T0 + i * MINUTE
    close = price * (1 + _PRICE_JITTER[i % len(_PRICE_JITTER)])
    book_at(con, bar, price, depth_qty, symbol, drift_to=close)
    notional = 500.0 * _VOLUME_JITTER[i % len(_VOLUME_JITTER)]
    trades_at(con, bar, price, notional, notional, symbol)


def quiet_market(n=140, **kw):
    con = make_con()
    for i in range(n):
        quiet_bar(con, i, **kw)
    return con


def gate(con, gate_cfg=None, **feature_overrides):
    """A gate over an in-memory archive, with windows sized for a test.

    The production windows (60-bar references, 60-bar observation) would need
    a four-hour fixture per test. The *thresholds* are never overridden -
    those are the thing under test.
    """
    settings = dict(volume_ref_bars=60, depth_ref_bars=60,
                    pump_window_bars=10, baseline_bars=10)
    settings.update(feature_overrides)
    fx = build(con, F.FeatureConfig(**settings))
    cfg = gate_cfg or PD.GateConfig(spike_ref_bars=60, observation_bars=15)
    return PD.PumpDumpGate(fx, cfg)


def bars_for(g, symbol="TEST", first=0, last=140):
    return g.extractor.bars(symbol, T0 + first * MINUTE, T0 + last * MINUTE,
                            include_walls=False)


# --------------------------------------------------------------------------
# the shapes
# --------------------------------------------------------------------------

PUMP_AT = 100          # bar index where the vertical leg happens


def add_pump(con, at=PUMP_AT, peak=112.0, base=100.0, depth_qty=4.0,
             buy_usd=60_000.0, sell_usd=1_500.0, bars=2, symbol="TEST"):
    """A vertical leg: thin book, huge one-sided buying, price straight up.

    The four statistical legs are set independently and generously - the
    point of this fixture is that a gate which is wired up correctly fires on
    it, not that these particular numbers are realistic.
    """
    for j in range(bars):
        bar = T0 + (at + j) * MINUTE
        lo = base + (peak - base) * j / bars
        hi = base + (peak - base) * (j + 1) / bars
        book_at(con, bar, lo, depth_qty, symbol, drift_to=hi)
        trades_at(con, bar, hi, buy_usd, sell_usd, symbol)


def add_dump(con, at, frm, to, bars=6, depth_qty=100.0, symbol="TEST"):
    """The round trip back down, on two-sided flow."""
    for j in range(bars):
        bar = T0 + (at + j) * MINUTE
        hi = frm + (to - frm) * j / bars
        lo = frm + (to - frm) * (j + 1) / bars
        book_at(con, bar, hi, depth_qty, symbol, drift_to=lo)
        trades_at(con, bar, lo, 3_000.0, 4_000.0, symbol)


def add_plateau(con, at, price, bars=20, depth_qty=100.0, symbol="TEST"):
    """Price holds where the pump left it."""
    for j in range(bars):
        bar = T0 + (at + j) * MINUTE
        quiet_bar(con, at + j, price=price, depth_qty=depth_qty, symbol=symbol)


@pytest.fixture
def clean_pump_and_dump():
    """The textbook shape: vertical leg, then a full round trip down."""
    con = make_con()
    for i in range(PUMP_AT):
        quiet_bar(con, i)
    add_pump(con)
    add_dump(con, PUMP_AT + 2, 112.0, 100.2, bars=8)
    for i in range(PUMP_AT + 10, 140):
        quiet_bar(con, i, price=100.2)
    return con


@pytest.fixture
def pump_that_never_dumps():
    """Identical vertical leg; price then holds. A re-rating, not a dump."""
    con = make_con()
    for i in range(PUMP_AT):
        quiet_bar(con, i)
    add_pump(con)
    add_plateau(con, PUMP_AT + 2, 112.0, bars=38)
    return con


# --------------------------------------------------------------------------
# candidate generation
# --------------------------------------------------------------------------

def test_quiet_market_produces_no_candidates():
    """The null case. If this ever fails, nothing below means anything."""
    g = gate(quiet_market())
    assert g.evaluate(bars_for(g)) == []


def test_clean_pump_produces_a_candidate(clean_pump_and_dump):
    g = gate(clean_pump_and_dump)
    candidates = g.evaluate(bars_for(g))
    assert candidates, "the textbook shape must clear the conjunction"
    first = candidates[0]
    assert first.symbol == "TEST"
    assert T0 + PUMP_AT * MINUTE <= first.bar_start <= T0 + (PUMP_AT + 1) * MINUTE
    assert first.volume_z > 5.0
    assert first.price_spike_z > 5.0
    assert first.thin_book_pctile < 0.20
    assert first.ofi >= 0.70
    assert first.move_pct > 0.05


def test_every_leg_of_the_conjunction_is_load_bearing(clean_pump_and_dump):
    """Each leg alone must be able to veto. A conjunction where one term does
    nothing is three tests pretending to be four."""
    base = gate(clean_pump_and_dump)
    assert base.evaluate(bars_for(base))

    for field, value in [("price_spike_z", 1e9), ("ofi_min", 1.0)]:
        cfg = dataclasses.replace(base.config, **{field: value})
        g = gate(clean_pump_and_dump, gate_cfg=cfg)
        assert not g.evaluate(bars_for(g)), f"{field} did not veto"

    for field, value in [("volume_notable_z", 1e9), ("thin_book_pctile", 0.0)]:
        g = gate(clean_pump_and_dump, **{field: value})
        assert not g.evaluate(bars_for(g)), f"{field} did not veto"


def test_slow_organic_rally_is_not_a_candidate():
    """Same 12% move, spread over two hours, on ordinary volume and a deep
    book. This is what the gate must *not* call - it is how a market prices
    in good news, and a detector that flags it is useless."""
    con = make_con()
    for i in range(40):
        quiet_bar(con, i)
    for j in range(80):
        price = 100.0 * (1.12 ** (j / 80))
        bar = T0 + (40 + j) * MINUTE
        nxt = 100.0 * (1.12 ** ((j + 1) / 80))
        book_at(con, bar, price, 100.0, drift_to=nxt)
        trades_at(con, bar, price, 700.0, 500.0)
    for i in range(120, 140):
        quiet_bar(con, i, price=112.0)

    g = gate(con)
    assert g.evaluate(bars_for(g)) == []


def test_volume_spike_without_price_movement_is_not_a_candidate():
    """Huge one-sided volume into a thin book, and price does not move.
    Volume z alone is not a pump - this is the single most common way a
    volume-threshold detector embarrasses itself."""
    con = make_con()
    for i in range(140):
        if i == PUMP_AT:
            bar = T0 + i * MINUTE
            book_at(con, bar, 100.0, 4.0)          # thin
            trades_at(con, bar, 100.0, 60_000.0, 1_500.0)   # huge, one-sided
        else:
            quiet_bar(con, i)

    g = gate(con)
    candidates = g.evaluate(bars_for(g))
    assert candidates == [], f"fired on a flat-price volume burst: {candidates}"


def test_dump_with_no_preceding_pump_is_not_a_candidate():
    """A crash. Volume and thinness spike, but the move is *down* and the
    flow is sell-side, so neither the spike leg nor the OFI leg is satisfied.
    Keeping the sign on both is what separates the two."""
    con = make_con()
    for i in range(PUMP_AT):
        quiet_bar(con, i)
    for j in range(4):
        bar = T0 + (PUMP_AT + j) * MINUTE
        hi = 100.0 - 3.0 * j
        book_at(con, bar, hi, 4.0, drift_to=hi - 3.0)
        trades_at(con, bar, hi, 1_500.0, 60_000.0)
    for i in range(PUMP_AT + 4, 140):
        quiet_bar(con, i, price=88.0)

    g = gate(con)
    assert g.evaluate(bars_for(g)) == []


def test_deep_book_during_the_pump_is_not_a_candidate(clean_pump_and_dump):
    """Same pump, but into a book at its usual depth.

    Thin books are *targets*: the leg exists because a manipulator picks a
    pair that is cheap to move. A move of this size through normal depth is
    someone spending real money, which is a different thing."""
    con = make_con()
    for i in range(PUMP_AT):
        quiet_bar(con, i)
    add_pump(con, depth_qty=100.0)        # the one difference
    add_dump(con, PUMP_AT + 2, 112.0, 100.2, bars=8)
    for i in range(PUMP_AT + 10, 140):
        quiet_bar(con, i, price=100.2)

    g = gate(con)
    assert g.evaluate(bars_for(g)) == []


def test_two_sided_flow_during_the_pump_is_not_a_candidate():
    """Price runs up on volume that is being met by sellers. That is a
    market, not one participant lifting a thin book."""
    con = make_con()
    for i in range(PUMP_AT):
        quiet_bar(con, i)
    add_pump(con, buy_usd=32_000.0, sell_usd=29_000.0)
    add_dump(con, PUMP_AT + 2, 112.0, 100.2, bars=8)
    for i in range(PUMP_AT + 10, 140):
        quiet_bar(con, i, price=100.2)

    g = gate(con)
    candidates = g.evaluate(bars_for(g))
    assert candidates == [], f"fired on balanced flow: {candidates}"


def test_uncovered_bars_never_become_candidates():
    """A recorder outage must not read as a market event. The bars are
    returned (so the hole is visible) and scored as NO_DATA, not as a minute
    with no volume and no depth."""
    con = make_con()
    for i in range(140):
        if 95 <= i < 105:
            continue                       # the recorder was down
        quiet_bar(con, i)

    g = gate(con)
    bars = bars_for(g)
    hole = [b for b in bars if 95 <= (b.bar_start - T0) // MINUTE < 105]
    assert hole and all(not b.covered for b in hole)
    assert g.evaluate(bars) == []


def test_partial_history_travels_with_the_candidate(clean_pump_and_dump):
    """A candidate scored off a half-length window is still emitted - going
    quiet after a restart is the wrong failure - but it says so."""
    g = gate(clean_pump_and_dump)
    for c in g.evaluate(bars_for(g)):
        assert c.status in (F.Status.OK, F.Status.ZERO_SCALE_FALLBACK,
                            F.Status.PARTIAL_HISTORY)

    strict = PD.GateConfig(spike_ref_bars=60, observation_bars=15,
                           max_status=F.Status.OK)
    g2 = gate(clean_pump_and_dump, gate_cfg=strict)
    assert all(c.status is F.Status.OK for c in g2.evaluate(bars_for(g2)))


# --------------------------------------------------------------------------
# the vol-normalised spike, on its own
# --------------------------------------------------------------------------

def test_the_same_move_scores_differently_on_a_placid_and_a_volatile_pair():
    """The entire reason the spike is normalised rather than a percentage."""
    cfg = PD.GateConfig(spike_window_bars=5, spike_ref_bars=60)
    fcfg = F.FeatureConfig()

    def synthetic(sigma: float) -> float:
        """A symbol whose only distinguishing property is how much it moves.

        Built without ``quiet_bar`` so ``sigma`` is the sole source of price
        scale - the shared fixture carries 8bps of its own noise, which would
        swamp a genuinely placid pair and quietly turn this into a test of
        nothing.
        """
        rng = random.Random(1)
        con = make_con()
        price = 100.0
        for i in range(90):
            nxt = price * (1 + rng.gauss(0.0, sigma))
            book_at(con, T0 + i * MINUTE, price, 100.0, drift_to=nxt)
            trades_at(con, T0 + i * MINUTE, price, 500.0, 500.0)
            price = nxt
        for j in range(3):                       # the same +3% in both
            bar = T0 + (90 + j) * MINUTE
            nxt = price * 1.03 ** (1 / 3)
            book_at(con, bar, price, 100.0, drift_to=nxt)
            trades_at(con, bar, price, 500.0, 500.0)
            price = nxt
        fx = build(con, fcfg)
        bars = fx.bars("TEST", T0, T0 + 95 * MINUTE, include_walls=False)
        zs = PD.price_spike_zs(bars, cfg, fcfg)
        return max(z.require() for z in zs if z.ok)

    placid = synthetic(0.0002)      # 2bps per minute
    volatile = synthetic(0.02)      # 200bps per minute
    # Same +3%: colossal on the placid pair, unremarkable on the volatile one.
    assert placid > 20.0, placid
    assert volatile < 5.0, volatile


def test_price_spike_keeps_its_sign():
    """A crash must not satisfy a pump's spike leg."""
    con = make_con()
    for i in range(90):
        quiet_bar(con, i)
    for j in range(5):
        quiet_bar(con, 90 + j, price=100.0 - 4.0 * (j + 1))
    fcfg = F.FeatureConfig()
    fx = build(con, fcfg)
    bars = fx.bars("TEST", T0, T0 + 95 * MINUTE, include_walls=False)
    zs = PD.price_spike_zs(bars, PD.GateConfig(), fcfg)
    assert min(z.require() for z in zs if z.ok) < -5.0
    assert max(z.require() for z in zs if z.ok) < 5.0


def test_window_returns_do_not_bridge_a_gap():
    """Carrying the last close across an un-recorded stretch would invent a
    move of exactly zero over a hole of unknown length."""
    con = make_con()
    for i in range(30):
        if 10 <= i < 20:
            continue
        quiet_bar(con, i)
    fx = build(con, F.FeatureConfig())
    bars = fx.bars("TEST", T0, T0 + 30 * MINUTE, include_walls=False)
    returns = PD.window_returns(bars, 5)
    for i, b in enumerate(bars):
        idx = (b.bar_start - T0) // MINUTE
        if 10 <= idx < 25:        # the hole, plus the five bars reaching into it
            assert returns[i] is None, idx


# --------------------------------------------------------------------------
# probabilistic vs confirmed: the structural boundary
# --------------------------------------------------------------------------

def test_a_candidate_has_no_retracement_and_no_confirmed_flag():
    """The type itself must not be able to express a finding."""
    fields = {f.name for f in dataclasses.fields(PD.Candidate)}
    for forbidden in ("retracement", "confirmed", "verdict", "trough", "peak"):
        assert forbidden not in fields, forbidden


def test_a_candidate_cannot_grow_a_confirmation_attribute(clean_pump_and_dump):
    """frozen: no code path can staple a verdict onto a live alert, and
    reading one that was never set fails loudly rather than as None."""
    g = gate(clean_pump_and_dump)
    c = g.evaluate(bars_for(g))[0]
    for name in ("retracement", "confirmed", "trough"):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(c, name, 1)
        with pytest.raises(AttributeError):
            getattr(c, name)


def test_a_confirmed_event_does_not_impersonate_a_candidate(clean_pump_and_dump):
    """Renderer written for one type breaks loudly on the other rather than
    silently relabelling it."""
    g = gate(clean_pump_and_dump)
    bars = bars_for(g)
    c = g.evaluate(bars)[0]
    event = PD.confirm(c, bars, g.config, g.extractor.config)
    assert isinstance(event, PD.ConfirmedDump)
    assert not isinstance(event, PD.Candidate)
    with pytest.raises(AttributeError):
        event.volume_z            # must be reached through .candidate
    assert event.candidate.volume_z == c.volume_z


def test_headlines_state_their_epistemic_status(clean_pump_and_dump):
    g = gate(clean_pump_and_dump)
    bars = bars_for(g)
    c = g.evaluate(bars)[0]
    assert "CANDIDATE" in c.headline() and "unconfirmed" in c.headline()
    event = PD.confirm(c, bars, g.config, g.extractor.config)
    assert "CONFIRMED" in event.headline()
    assert "CANDIDATE" not in event.headline()


def test_confirmation_refuses_the_candidates_own_window(clean_pump_and_dump):
    """The structural guarantee. Handed only the bars that existed when the
    alert fired, confirmation raises rather than returning a degraded
    answer - the trough is in the future and no statistic can fix that."""
    g = gate(clean_pump_and_dump)
    bars = bars_for(g)
    c = g.evaluate(bars)[0]
    live = [b for b in bars if b.bar_start <= c.bar_start]
    with pytest.raises(PD.PrematureConfirmation):
        PD.confirm(c, live, g.config, g.extractor.config)


def test_confirmation_refuses_a_half_observed_window(clean_pump_and_dump):
    g = gate(clean_pump_and_dump)
    bars = bars_for(g)
    c = g.evaluate(bars)[0]
    cutoff = c.bar_start + (g.config.observation_bars - 1) * MINUTE
    with pytest.raises(PD.PrematureConfirmation):
        PD.confirm(c, [b for b in bars if b.bar_start <= cutoff],
                   g.config, g.extractor.config)


def test_confirmation_refuses_a_missing_baseline(clean_pump_and_dump):
    """Measuring a retracement against a baseline that was never recorded
    produces a number with no meaning, so it raises rather than inventing
    one."""
    g = gate(clean_pump_and_dump)
    bars = bars_for(g)
    c = g.evaluate(bars)[0]
    fcfg = g.extractor.config
    start = c.bar_start - fcfg.baseline_bars * MINUTE
    gutted = [b for b in bars
              if not (start <= b.bar_start < c.bar_start)
              or b.bar_start >= c.bar_start - 2 * MINUTE]
    with pytest.raises((F.InsufficientHistory, PD.PrematureConfirmation)):
        PD.confirm(c, gutted, g.config, fcfg)


# --------------------------------------------------------------------------
# confirmation, on the shapes
# --------------------------------------------------------------------------

def test_clean_pump_and_dump_confirms(clean_pump_and_dump):
    g = gate(clean_pump_and_dump)
    bars = bars_for(g)
    c = g.evaluate(bars)[0]
    event = PD.confirm(c, bars, g.config, g.extractor.config)
    assert event is not None
    assert event.retracement > 0.70
    assert event.runup_pct > 0.05
    assert event.peak > event.trough
    assert event.trough_at > c.bar_start        # the trough follows the peak


def test_pump_that_never_dumps_does_not_confirm(pump_that_never_dumps):
    """The discriminating case, and the reason confirmation is a separate
    step: the *candidate* is identical - the same four leading indicators
    fired - and only the future tells them apart."""
    g = gate(pump_that_never_dumps)
    bars = bars_for(g)
    candidates = g.evaluate(bars)
    assert candidates, "the leading indicators are the same as the dump case"
    event = PD.confirm(candidates[0], bars, g.config, g.extractor.config)
    assert event is None, "a plateau is not a retracement"


def test_confirmation_returns_none_rather_than_raising_when_it_looked():
    """``None`` means 'looked, and no'. The exception means 'cannot look
    yet'. Collapsing the two would make a held pump indistinguishable from
    an alert that is still too fresh to judge."""
    con = make_con()
    for i in range(PUMP_AT):
        quiet_bar(con, i)
    add_pump(con)
    add_plateau(con, PUMP_AT + 2, 112.0, bars=38)
    g = gate(con)
    bars = bars_for(g)
    c = g.evaluate(bars)[0]
    assert PD.confirm(c, bars, g.config, g.extractor.config) is None


# --------------------------------------------------------------------------
# the catalyst seam
# --------------------------------------------------------------------------

def test_the_default_catalyst_source_says_nobody_looked(clean_pump_and_dump):
    """Not ABSENT. An unchecked candidate must not wear the no-catalyst
    signal it never earned."""
    g = gate(clean_pump_and_dump)
    c = g.evaluate(bars_for(g))[0]
    assert c.catalyst is PD.Catalyst.UNKNOWN


def test_a_present_catalyst_suppresses_the_candidate(clean_pump_and_dump):
    class Newsy(PD.CatalystSource):
        def check(self, symbol, start_ms, end_ms):
            return PD.Catalyst.PRESENT

    fx = build(clean_pump_and_dump, F.FeatureConfig(
        volume_ref_bars=60, depth_ref_bars=60,
        pump_window_bars=10, baseline_bars=10))
    cfg = PD.GateConfig(spike_ref_bars=60, observation_bars=15)
    assert PD.PumpDumpGate(fx, cfg, Newsy()).evaluate(
        fx.bars("TEST", T0, T0 + 140 * MINUTE, include_walls=False)) == []


def test_an_absent_catalyst_is_recorded_on_the_candidate(clean_pump_and_dump):
    class Quiet(PD.CatalystSource):
        def check(self, symbol, start_ms, end_ms):
            return PD.Catalyst.ABSENT

    fx = build(clean_pump_and_dump, F.FeatureConfig(
        volume_ref_bars=60, depth_ref_bars=60,
        pump_window_bars=10, baseline_bars=10))
    cfg = PD.GateConfig(spike_ref_bars=60, observation_bars=15)
    candidates = PD.PumpDumpGate(fx, cfg, Quiet()).evaluate(
        fx.bars("TEST", T0, T0 + 140 * MINUTE, include_walls=False))
    assert candidates and all(c.catalyst is PD.Catalyst.ABSENT
                              for c in candidates)


def test_catalyst_is_the_last_leg_checked(clean_pump_and_dump):
    """A news lookup is a rate-limited network call with a bill attached; it
    must only run on bars that already cleared four statistical tests."""
    calls = []

    class Counting(PD.CatalystSource):
        def check(self, symbol, start_ms, end_ms):
            calls.append(start_ms)
            return PD.Catalyst.ABSENT

    fx = build(clean_pump_and_dump, F.FeatureConfig(
        volume_ref_bars=60, depth_ref_bars=60,
        pump_window_bars=10, baseline_bars=10))
    cfg = PD.GateConfig(spike_ref_bars=60, observation_bars=15)
    bars = fx.bars("TEST", T0, T0 + 140 * MINUTE, include_walls=False)
    PD.PumpDumpGate(fx, cfg, Counting()).evaluate(bars)
    assert 0 < len(calls) <= 5, f"{len(calls)} news calls for {len(bars)} bars"


# --------------------------------------------------------------------------
# config and plumbing
# --------------------------------------------------------------------------

def test_gate_config_validates():
    with pytest.raises(ValueError):
        PD.GateConfig(spike_window_bars=0)
    with pytest.raises(ValueError):
        PD.GateConfig(ofi_min=0.0)
    with pytest.raises(ValueError):
        PD.GateConfig(ofi_min=1.5)


def test_frozen_thresholds_are_the_documented_design_values():
    """A guard against the failure mode this detector is most exposed to:
    nudging a threshold down until the archive produces a hit. The archive
    contains no pump, so anything it fires on is a false positive, and a
    change to these numbers should be a deliberate edit to this test."""
    cfg = PD.GateConfig()
    assert cfg.price_spike_z == 5.0
    assert cfg.ofi_min == 0.70
    assert cfg.spike_window_bars == 5
    fcfg = F.FeatureConfig()
    assert fcfg.volume_notable_z == 5.0
    assert fcfg.volume_extreme_z == 10.0
    assert fcfg.thin_book_pctile == 0.20
    assert fcfg.retracement_fires_at == 0.70


def test_severity_is_extreme_past_the_extreme_volume_cut(clean_pump_and_dump):
    g = gate(clean_pump_and_dump)
    c = g.evaluate(bars_for(g))[0]
    expected = (PD.Severity.EXTREME
                if c.volume_z > g.extractor.config.volume_extreme_z
                else PD.Severity.NOTABLE)
    assert c.severity is expected


def test_scan_does_not_emit_bars_before_the_requested_start(clean_pump_and_dump):
    g = gate(clean_pump_and_dump)
    start = T0 + (PUMP_AT + 1) * MINUTE
    for c in g.scan("TEST", start, T0 + 140 * MINUTE):
        assert c.bar_start >= start


def test_evaluate_needs_no_extractor(clean_pump_and_dump):
    """The gate is stateless with respect to the archive, so a future
    streaming feature source drops in without touching this module."""
    fx = build(clean_pump_and_dump, F.FeatureConfig(
        volume_ref_bars=60, depth_ref_bars=60,
        pump_window_bars=10, baseline_bars=10))
    bars = fx.bars("TEST", T0, T0 + 140 * MINUTE, include_walls=False)
    standalone = PD.PumpDumpGate(None, PD.GateConfig(spike_ref_bars=60))
    assert standalone.evaluate(bars)
    with pytest.raises(ValueError):
        standalone.scan("TEST", T0, T0 + 140 * MINUTE)


def test_distribution_reports_unusable_bars():
    g = gate(quiet_market())
    bars = bars_for(g)
    dists = {d.name: d for d in PD.describe(bars, g.config, g.extractor.config)}
    assert set(dists) == {"volume_z", "price_spike_z", "thin_book_p", "ofi",
                          "retracement"}
    for d in dists.values():
        assert d.n + d.n_unusable == len(bars)
        d.line()        # must render without raising, usable or not
