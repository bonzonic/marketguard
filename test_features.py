"""Tests for the feature extractor.

All fixtures here are synthetic. The recorded archive is irreplaceable, it is
being appended to by a live recorder, and its contents will change - none of
which belongs in a correctness test. Running features.py against real data is
a plausibility check, not a test.

The emphasis is on the statistics and on the three ways this module could be
wrong while still running happily: the inverted ``m`` flag flipping the sign
of order flow imbalance, a trailing window quietly built out of fabricated
zeros, and a level that left the visible book being scored as a cancellation.
"""
from __future__ import annotations

import gzip
import json
import math

import duckdb
import pyarrow as pa
import pytest

import etl
import features as F


# --------------------------------------------------------------------------
# pure statistics
# --------------------------------------------------------------------------

def test_median_and_mad_basic():
    assert F.median([1, 2, 3, 4, 5]) == 3
    assert F.median([1, 2, 3, 4]) == 2.5
    # deviations from median 3 are [2,1,0,1,2] -> median 1
    assert F.mad([1, 2, 3, 4, 5]) == 1


def test_median_and_mad_reject_empty():
    with pytest.raises(ValueError):
        F.median([])
    with pytest.raises(ValueError):
        F.mad([])


def test_mad_ignores_extreme_outliers_but_stdev_does_not():
    """The whole reason for MAD: past pumps must not raise the bar."""
    import statistics

    quiet = [8.0, 9.0, 10.0, 11.0, 12.0] * 4          # median 10, MAD 1
    with_pumps = quiet + [10_000.0, 12_000.0, 9_000.0]

    assert F.mad(quiet) == 1.0
    assert F.mad(with_pumps) == 1.0                    # three pumps move it none

    z_robust, status = F.robust_z(500.0, with_pumps)
    z_naive = (500.0 - statistics.mean(with_pumps)) / statistics.pstdev(with_pumps)

    assert status is F.Status.OK
    # A 50x burst is extreme on MAD and invisible on mean/stdev.
    assert z_robust == pytest.approx(490.0)
    assert abs(z_naive) < 1


def test_nonzero_mad_fallback_stays_robust():
    """The obvious fallback - mean absolute deviation - is not robust, and
    picking it would quietly undo the reason MAD is here."""
    import statistics

    ref = [10.0] * 20 + [10_000.0, 12_000.0, 9_000.0]
    assert F.mad(ref) == 0.0                           # fallback territory
    assert F.nonzero_mad(ref) == 9_990.0               # median of |dev| != 0
    mean_ad = sum(abs(v - 10.0) for v in ref) / len(ref)
    assert mean_ad < 1_500                             # dragged down by the 20 tens
    assert F.nonzero_mad(ref) > 6 * mean_ad
    assert statistics.pstdev(ref) > 0                  # and stdev is worse still


def test_nonzero_mad_is_zero_only_when_everything_is_identical():
    assert F.nonzero_mad([5.0] * 10) == 0.0
    assert F.nonzero_mad([0.0] * 8 + [4.0, 8.0]) == 6.0


def test_robust_z_sign_and_scale():
    ref = [10, 12, 8, 11, 9, 10, 10, 12, 8, 10]   # median 10, MAD 1
    z, status = F.robust_z(15, ref)
    assert status is F.Status.OK
    assert z == pytest.approx(5.0)
    z, _ = F.robust_z(5, ref)
    assert z == pytest.approx(-5.0)


def test_robust_z_falls_back_when_mad_is_zero():
    """More than half the window identical -> MAD 0. Common before a pump in
    an illiquid symbol, and the one moment we must not go blind."""
    ref = [0.0] * 8 + [4.0, 8.0]      # median 0, MAD 0, nonzero_mad 6
    z, status = F.robust_z(120.0, ref)
    assert status is F.Status.ZERO_SCALE_FALLBACK
    assert z == pytest.approx(20.0)


def test_robust_z_gives_up_when_every_observation_is_identical():
    z, status = F.robust_z(5.0, [3.0] * 10)
    assert z is None
    assert status is F.Status.ZERO_SCALE


def test_percentile_rank_endpoints_and_midrank():
    ref = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert F.percentile_rank(0, ref) == 0.0
    assert F.percentile_rank(11, ref) == 1.0
    assert F.percentile_rank(5.5, ref) == pytest.approx(0.5)
    # A value equal to one observation gets half credit for the tie.
    assert F.percentile_rank(5, ref) == pytest.approx(0.45)


def test_percentile_rank_all_ties_is_one_half():
    """With `<=` this would be 1.0 and thin-book would never fire; with `<`
    it would be 0.0 and it would fire on every bar."""
    assert F.percentile_rank(7, [7] * 50) == pytest.approx(0.5)


def test_percentile_rank_detects_thin_book():
    ref = list(range(100, 200))
    assert F.percentile_rank(105, ref) < 0.20
    assert F.percentile_rank(150, ref) > 0.20


def test_percentile_rank_rejects_empty_reference():
    with pytest.raises(ValueError):
        F.percentile_rank(1.0, [])


def test_quantile_matches_duckdb_quantile_cont():
    """Python-side and SQL-side thresholds must use the same definition."""
    values = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0, 5.0, 3.0, 5.0]
    con = duckdb.connect()
    for q in (0.0, 0.2, 0.5, 0.9, 0.99, 1.0):
        expected = con.execute(
            "SELECT quantile_cont(x, ?) FROM (SELECT unnest(?::DOUBLE[]) AS x)",
            [q, values],
        ).fetchone()[0]
        assert F.quantile(values, q) == pytest.approx(expected), q


def test_quantile_rejects_bad_input():
    with pytest.raises(ValueError):
        F.quantile([], 0.5)
    with pytest.raises(ValueError):
        F.quantile([1.0], 1.5)


# --------------------------------------------------------------------------
# order flow imbalance - the inverted `m` flag
# --------------------------------------------------------------------------

def test_ofi_sign_is_positive_for_aggressive_buying():
    assert F.ofi(100.0, 0.0) == 1.0
    assert F.ofi(0.0, 100.0) == -1.0
    assert F.ofi(50.0, 50.0) == 0.0
    assert F.ofi(75.0, 25.0) == pytest.approx(0.5)


def test_ofi_is_none_when_nothing_traded():
    assert F.ofi(0.0, 0.0) is None


def test_etl_maps_m_false_to_aggressive_buy(tmp_path):
    """`m: false` means the BUYER crossed the spread.

    The field reads as though it means the opposite, and inverting it flips
    the sign of every OFI in the system while everything downstream keeps
    running. This pins the convention at the point it is decided.
    """
    path = tmp_path / "binance_20260101T00.jsonl.gz"
    msgs = [
        {"t": 1000, "stream": "solusdt@aggTrade",
         "data": {"e": "aggTrade", "E": 1000, "s": "SOLUSDT", "a": 1,
                  "p": "10.0", "q": "3.0", "m": False}},
        {"t": 2000, "stream": "solusdt@aggTrade",
         "data": {"e": "aggTrade", "E": 2000, "s": "SOLUSDT", "a": 2,
                  "p": "10.0", "q": "1.0", "m": True}},
    ]
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for m in msgs:
            fh.write(json.dumps(m) + "\n")

    _book, trades = etl.convert_hour([path])
    rows = trades.to_pylist()
    assert rows[0]["is_buyer_maker"] is False
    assert rows[0]["aggressive_buy"] is True     # m: false -> taker bought
    assert rows[1]["is_buyer_maker"] is True
    assert rows[1]["aggressive_buy"] is False    # m: true  -> taker sold

    buy = sum(r["qty"] for r in rows if r["aggressive_buy"])
    sell = sum(r["qty"] for r in rows if not r["aggressive_buy"])
    assert F.ofi(buy, sell) == pytest.approx(0.5)


# --------------------------------------------------------------------------
# retracement
# --------------------------------------------------------------------------

def test_retracement_full_round_trip_is_one():
    highs = [100, 100, 150, 140, 100]
    lows = [100, 100, 140, 120, 100]
    ratio, peak, trough = F.retracement(highs, lows, baseline=100)
    assert peak == 150
    assert trough == 100
    assert ratio == pytest.approx(1.0)


def test_retracement_partial():
    highs = [100, 120, 110]
    lows = [100, 118, 106]
    ratio, _, _ = F.retracement(highs, lows, baseline=100)
    assert ratio == pytest.approx((120 - 106) / (120 - 100))


def test_retracement_only_looks_after_the_peak():
    """A low that came *before* the run-up is not a retracement of it.

    Taking min() over the whole window turns every ordinary oscillation into
    a confirmed dump.
    """
    highs = [100, 100, 130]
    lows = [10, 100, 129]            # the 10 precedes the peak
    ratio, peak, trough = F.retracement(highs, lows, baseline=100)
    assert peak == 130
    assert trough == 129
    assert ratio == pytest.approx((130 - 129) / 30)


def test_retracement_none_without_a_runup():
    ratio, _, _ = F.retracement([99, 98], [95, 94], baseline=100)
    assert ratio is None


def test_retracement_skips_uncovered_bars():
    ratio, peak, trough = F.retracement([None, 150, None], [None, 100, None], 100)
    assert (peak, trough) == (150, 100)
    assert ratio == pytest.approx(1.0)


def test_retracement_empty_window():
    assert F.retracement([None], [None], 100) == (None, None, None)


# --------------------------------------------------------------------------
# status plumbing
# --------------------------------------------------------------------------

def test_history_status_degrades_in_three_steps():
    assert F._history_status(60, 60, 0.5) is F.Status.OK
    assert F._history_status(80, 60, 0.5) is F.Status.OK
    assert F._history_status(40, 60, 0.5) is F.Status.PARTIAL_HISTORY
    assert F._history_status(10, 60, 0.5) is F.Status.INSUFFICIENT_HISTORY
    assert F._history_status(0, 60, 0.5) is F.Status.INSUFFICIENT_HISTORY


def test_windowed_require_refuses_to_hand_back_a_missing_value():
    w = F.Windowed(None, F.Status.INSUFFICIENT_HISTORY, 3, 60)
    assert not w.ok
    with pytest.raises(F.InsufficientHistory):
        w.require()
    assert F.Windowed(1.5, F.Status.OK, 60, 60).require() == 1.5


def test_partial_history_is_still_usable():
    assert F.Windowed(1.0, F.Status.PARTIAL_HISTORY, 40, 60).ok


def test_worst_status_picks_the_weaker_window():
    assert F.worst(F.Status.OK, F.Status.PARTIAL_HISTORY) is F.Status.PARTIAL_HISTORY
    assert F.worst(F.Status.PARTIAL_HISTORY,
                   F.Status.INSUFFICIENT_HISTORY) is F.Status.INSUFFICIENT_HISTORY
    assert F.worst(F.Status.OK, F.Status.OK) is F.Status.OK


def test_config_validates():
    with pytest.raises(ValueError):
        F.FeatureConfig(trade_clock="exchange")
    with pytest.raises(ValueError):
        F.FeatureConfig(volume_ref_bars=0)


def test_config_lookback_covers_the_widest_window():
    cfg = F.FeatureConfig(volume_ref_bars=10, depth_ref_bars=20,
                          pump_window_bars=30, baseline_bars=15)
    assert cfg.lookback_bars == 45
    assert cfg.lookback_ms == 45 * 60_000


# --------------------------------------------------------------------------
# synthetic archive
# --------------------------------------------------------------------------

MINUTE = 60_000
T0 = 1_700_000_000_000 // MINUTE * MINUTE


# Rows are buffered and inserted with executemany at build() time. One
# INSERT per snapshot is a transaction per snapshot in DuckDB, which took the
# suite from seconds to twelve minutes.
_PENDING: dict[int, dict[str, list]] = {}


_SCHEMAS = {"book": etl.BOOK_SCHEMA, "trades": etl.TRADE_SCHEMA}


def make_con() -> duckdb.DuckDBPyConnection:
    """An empty in-memory database shaped exactly like the parquet views.

    The tables are built from etl.py's own Arrow schemas rather than a second
    copy of the DDL, so a change to the recorded columns breaks these tests
    instead of silently testing a shape that no longer exists.
    """
    con = duckdb.connect()
    for name, schema in _SCHEMAS.items():
        con.register(f"_schema_{name}", schema.empty_table())
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _schema_{name}")
        con.unregister(f"_schema_{name}")
    _PENDING[id(con)] = {"trades": [], "book": []}
    return con


def add_trade(con, t, price, qty, aggressive_buy, symbol="TEST"):
    _PENDING[id(con)]["trades"].append(
        [t, t, symbol, t, price, qty, not aggressive_buy, aggressive_buy])


def add_snapshot(con, t, bids, asks, symbol="TEST", est=False):
    """bids/asks are [(price, qty), ...], best first."""
    mid = (bids[0][0] + asks[0][0]) / 2
    _PENDING[id(con)]["book"].append(
        [t, symbol, t, bids[0][0], bids[0][1], asks[0][0], asks[0][1],
         mid, asks[0][0] - bids[0][0],
         sum(q for _, q in bids), sum(q for _, q in asks),
         [{"price": p, "qty": q} for p, q in bids],
         [{"price": p, "qty": q} for p, q in asks],
         est])


def flush(con):
    """Bulk-load the buffered rows through Arrow.

    Row-at-a-time INSERT costs ~5ms per row in DuckDB whether you use
    execute or executemany, which is a ten-minute test suite.
    """
    pending = _PENDING.get(id(con))
    if not pending:
        return
    for name, rows in pending.items():
        if not rows:
            continue
        schema = _SCHEMAS[name]
        table = pa.table(
            {f.name: [row[i] for row in rows] for i, f in enumerate(schema)},
            schema=schema,
        )
        con.register(f"_load_{name}", table)
        con.execute(f"INSERT INTO {name} SELECT * FROM _load_{name}")
        con.unregister(f"_load_{name}")
        rows.clear()


def build(con, cfg) -> F.BatchFeatureExtractor:
    flush(con)
    return F.BatchFeatureExtractor(con=con, cfg=cfg)


def flat_book(con, bar_start, symbol="TEST", price=100.0, qty=10.0, n=4,
              snapshots=4):
    """A boring, symmetric, unchanging book for one bar."""
    step = MINUTE // snapshots
    for k in range(snapshots):
        bids = [(price - 0.01 * (i + 1), qty) for i in range(n)]
        asks = [(price + 0.01 * (i + 1), qty) for i in range(n)]
        add_snapshot(con, bar_start + k * step, bids, asks, symbol)


@pytest.fixture
def quiet_market():
    """120 identical minutes: $1,000 of balanced volume, unchanging book."""
    con = make_con()
    for i in range(120):
        bar = T0 + i * MINUTE
        flat_book(con, bar)
        add_trade(con, bar + 1_000, 100.0, 5.0, True)
        add_trade(con, bar + 2_000, 100.0, 5.0, False)
    return con


def extractor(con, **overrides):
    settings = dict(volume_ref_bars=60, depth_ref_bars=60,
                    pump_window_bars=10, baseline_bars=10)
    settings.update(overrides)
    return build(con, F.FeatureConfig(**settings))


def test_quiet_market_produces_no_flags(quiet_market):
    fx = extractor(quiet_market)
    bars = fx.bars("TEST", T0 + 60 * MINUTE, T0 + 120 * MINUTE, include_walls=False)
    assert len(bars) == 60
    assert all(b.covered for b in bars)
    assert all(b.volume_usd == pytest.approx(1000.0) for b in bars)
    # Every bar identical -> no scale at all, and we say so rather than
    # inventing a z of 0.
    assert all(b.volume_z.status is F.Status.ZERO_SCALE for b in bars)
    assert all(b.ofi.require() == pytest.approx(0.0) for b in bars)
    assert all(not b.flags(fx.config) for b in bars)


def test_volume_spike_scores_extreme(quiet_market):
    con = quiet_market
    spike = T0 + 100 * MINUTE
    # Vary the baseline slightly so MAD is non-zero, then add a burst.
    for i in range(120):
        add_trade(con, T0 + i * MINUTE + 3_000, 100.0, 0.1 * (i % 5), True)
    add_trade(con, spike + 10_000, 100.0, 400.0, True)

    fx = extractor(con)
    bars = {b.bar_start: b for b in
            fx.bars("TEST", T0 + 90 * MINUTE, T0 + 110 * MINUTE, include_walls=False)}
    hit = bars[spike]
    assert hit.volume_z.status in (F.Status.OK, F.Status.ZERO_SCALE_FALLBACK)
    assert hit.volume_z.require() > 10
    assert "volume_extreme" in hit.flags(fx.config)
    # The very next bar must not inherit it.
    assert "volume_extreme" not in bars[spike + MINUTE].flags(fx.config)


def test_uncovered_bars_are_not_zero_volume_bars():
    """A minute the recorder never saw must not enter the trailing window.

    Left-join a dense grid onto trades and every missing minute becomes a
    0.0; a window half full of fabricated zeros drags the median to 0, the
    MAD to 0, and the z-score to nonsense. This walks the full degradation
    through a 60-minute outage: NO_DATA inside it, INSUFFICIENT_HISTORY the
    moment recording resumes, PARTIAL_HISTORY as the window refills, OK once
    it is full again.
    """
    con = make_con()
    for i in range(180):
        if 40 <= i < 100:            # recorder down for 60 minutes
            continue
        bar = T0 + i * MINUTE
        flat_book(con, bar)
        add_trade(con, bar + 1_000, 100.0, 5.0 + (i % 3), True)

    fx = extractor(con)
    bars = {b.bar_start: b for b in
            fx.bars("TEST", T0 + 40 * MINUTE, T0 + 180 * MINUTE, include_walls=False)}

    gap_bar = bars[T0 + 50 * MINUTE]
    assert not gap_bar.covered
    assert gap_bar.volume_usd == 0.0        # the left join really does say 0
    assert gap_bar.volume_z.status is F.Status.NO_DATA
    assert gap_bar.volume_z.value is None
    assert not gap_bar.flags(fx.config)

    # The trailing window is 60 bars of wall clock, not the last 60 recorded
    # bars: after an hour-long outage there is no recent baseline at all, and
    # reaching back across the hole to compare against hour-old volume would
    # be worse than admitting it.
    resumed = bars[T0 + 100 * MINUTE]
    assert resumed.covered
    assert resumed.volume_z.n_obs == 0
    assert resumed.volume_z.status is F.Status.INSUFFICIENT_HISTORY

    refilling = bars[T0 + 140 * MINUTE]
    assert refilling.volume_z.n_obs == 40
    assert refilling.volume_z.status is F.Status.PARTIAL_HISTORY

    recovered = bars[T0 + 165 * MINUTE]
    assert recovered.volume_z.n_obs == 60
    assert recovered.volume_z.status in (F.Status.OK, F.Status.ZERO_SCALE_FALLBACK)


def test_insufficient_history_is_explicit_not_zero():
    con = make_con()
    for i in range(5):
        bar = T0 + i * MINUTE
        flat_book(con, bar)
        add_trade(con, bar + 1_000, 100.0, 5.0 + i, True)

    fx = extractor(con)
    bars = fx.bars("TEST", T0, T0 + 5 * MINUTE, include_walls=False)
    first = bars[0]
    assert first.volume_z.status is F.Status.INSUFFICIENT_HISTORY
    assert first.volume_z.value is None
    assert first.volume_z.n_obs == 0
    assert first.volume_z.n_required == 60
    with pytest.raises(F.InsufficientHistory):
        first.volume_z.require()


def test_ofi_window_reflects_one_sided_taker_buying():
    con = make_con()
    for i in range(20):
        bar = T0 + i * MINUTE
        flat_book(con, bar)
        add_trade(con, bar + 1_000, 100.0, 5.0, True)
        add_trade(con, bar + 2_000, 100.0, 5.0, False)
    # Five minutes of nothing but aggressive buying.
    for i in range(15, 20):
        add_trade(con, T0 + i * MINUTE + 3_000, 100.0, 100.0, True)

    fx = extractor(con, ofi_window_bars=5)
    bars = {b.bar_start: b for b in
            fx.bars("TEST", T0 + 10 * MINUTE, T0 + 20 * MINUTE, include_walls=False)}
    assert bars[T0 + 12 * MINUTE].ofi.require() == pytest.approx(0.0)
    assert bars[T0 + 19 * MINUTE].ofi.require() > 0.9


def test_thin_book_percentile_fires_when_depth_collapses():
    con = make_con()
    for i in range(100):
        bar = T0 + i * MINUTE
        qty = 10.0 + (i % 7)               # a real distribution to rank against
        if i == 90:
            qty = 0.5                      # the book empties out
        flat_book(con, bar, qty=qty)
        add_trade(con, bar + 1_000, 100.0, 1.0, True)

    fx = extractor(con)
    bars = {b.bar_start: b for b in
            fx.bars("TEST", T0 + 60 * MINUTE, T0 + 100 * MINUTE, include_walls=False)}
    thin = bars[T0 + 90 * MINUTE]
    assert thin.thin_book.require() < 0.20
    assert "thin_book" in thin.flags(fx.config)
    assert bars[T0 + 80 * MINUTE].thin_book.require() > 0.20


def test_depth_is_clamped_to_the_price_band():
    """Levels outside +/-1% of mid must not count toward 'depth near mid'."""
    con = make_con()
    for i in range(70):
        bar = T0 + i * MINUTE
        for k in range(4):
            bids = [(99.99, 1.0), (50.0, 10_000.0)]   # second level is 50% out
            asks = [(100.01, 1.0), (150.0, 10_000.0)]
            add_snapshot(con, bar + k * (MINUTE // 4), bids, asks)
        add_trade(con, bar + 1_000, 100.0, 1.0, True)

    fx = extractor(con)
    bars = fx.bars("TEST", T0 + 65 * MINUTE, T0 + 70 * MINUTE, include_walls=False)
    # 99.99 * 1 + 100.01 * 1, and nothing from the far levels.
    assert bars[0].depth_usd == pytest.approx(200.0)


def test_retracement_feature_end_to_end():
    con = make_con()
    # 20 flat bars at 100, a ramp to 120, then all the way back to 100.
    path = [100.0] * 20 + [104.0, 108.0, 112.0, 116.0, 120.0] + \
           [116.0, 112.0, 106.0, 100.0, 100.0]
    for i, price in enumerate(path):
        bar = T0 + i * MINUTE
        flat_book(con, bar, price=price)
        add_trade(con, bar + 1_000, price, 1.0, True)

    fx = extractor(con, pump_window_bars=10, baseline_bars=10)
    bars = {b.bar_start: b for b in
            fx.bars("TEST", T0 + 20 * MINUTE, T0 + 30 * MINUTE, include_walls=False)}

    mid_ramp = bars[T0 + 24 * MINUTE]      # at the peak, nothing retraced yet
    assert mid_ramp.retracement.require() < 0.2

    done = bars[T0 + 29 * MINUTE]          # round trip complete
    assert done.retracement.require() > 0.9
    assert "retracement" in done.flags(fx.config)


def test_retracement_reports_no_pump_rather_than_a_number():
    con = make_con()
    for i in range(40):
        bar = T0 + i * MINUTE
        flat_book(con, bar, price=100.0)
        add_trade(con, bar + 1_000, 100.0, 1.0, True)

    fx = extractor(con, pump_window_bars=10, baseline_bars=10)
    bars = fx.bars("TEST", T0 + 30 * MINUTE, T0 + 40 * MINUTE, include_walls=False)
    assert all(b.retracement.status is F.Status.NO_PUMP for b in bars)
    assert all(b.retracement.value is None for b in bars)


# --------------------------------------------------------------------------
# walls
# --------------------------------------------------------------------------

def wall_config(**overrides):
    base = dict(wall_sample_ms=1_000, wall_band_bps=5.0, wall_max_bps=50.0,
                wall_proximity_bps=20.0, wall_quantile=0.99,
                wall_ref_ms=10 * MINUTE, wall_min_ref_obs=50,
                volume_ref_bars=10, depth_ref_bars=10,
                pump_window_bars=5, baseline_bars=5)
    base.update(overrides)
    return F.FeatureConfig(**base)


def build_wall_market(con, wall_at: int | None, wall_qty=500.0, wall_level=1):
    """20 minutes of an even book; optionally one huge level for 30 seconds.

    ``wall_level`` is how many ticks from the top the wall sits. Ticks are
    0.01 on a mid of 100, i.e. 1bp each.
    """
    for s in range(20 * 60):
        t = T0 + s * 1_000
        bids = [(100.0 - 0.01 * (i + 1), 10.0) for i in range(20)]
        asks = [(100.0 + 0.01 * (i + 1), 10.0) for i in range(20)]
        if wall_at is not None and wall_at <= t < wall_at + 30_000:
            p, _ = bids[wall_level]
            bids[wall_level] = (p, wall_qty)
        add_snapshot(con, t, bids, asks)


def test_wall_fires_near_mid():
    con = make_con()
    wall_at = T0 + 15 * MINUTE
    build_wall_market(con, wall_at)
    fx = build(con, wall_config())

    events = fx.walls("TEST", T0 + 10 * MINUTE, T0 + 20 * MINUTE)
    assert len(events) == 1
    w = events[0]
    assert w.side == "bid"
    assert w.price == pytest.approx(99.98)
    assert w.max_usd == pytest.approx(99.98 * 500.0)
    assert w.max_ratio > 10
    assert w.min_bps <= 20
    assert 25_000 <= w.duration_ms <= 30_000


def test_wall_does_not_fire_beyond_the_proximity_band():
    """Proximity is part of the definition - a wall 40bps out persuades
    nobody, and reporting it produces false spoofing calls all day."""
    con = make_con()
    build_wall_market(con, T0 + 15 * MINUTE, wall_level=19)   # ~20bps out
    fx = build(con, wall_config(wall_proximity_bps=5.0))
    assert fx.walls("TEST", T0 + 10 * MINUTE, T0 + 20 * MINUTE) == []


def test_no_wall_in_an_even_book():
    con = make_con()
    build_wall_market(con, None)
    fx = build(con, wall_config())
    assert fx.walls("TEST", T0 + 10 * MINUTE, T0 + 20 * MINUTE) == []


def test_wall_reference_shortfall_is_reported_not_silently_empty():
    con = make_con()
    build_wall_market(con, None)
    fx = build(con, wall_config(wall_min_ref_obs=10_000_000))
    bars = fx.bars("TEST", T0 + 10 * MINUTE, T0 + 12 * MINUTE)
    assert bars
    assert all(b.wall_ratio.status is F.Status.INSUFFICIENT_HISTORY for b in bars)
    assert all(b.wall_fires == 0 for b in bars)


def test_wall_bar_aggregate_lines_up_with_the_events():
    con = make_con()
    wall_at = T0 + 15 * MINUTE
    build_wall_market(con, wall_at)
    fx = build(con, wall_config())
    bars = {b.bar_start: b for b in fx.bars("TEST", T0 + 14 * MINUTE, T0 + 17 * MINUTE)}
    assert bars[wall_at].wall_fires > 0
    assert "wall" in bars[wall_at].flags(fx.config)
    assert bars[T0 + 14 * MINUTE].wall_fires == 0
    assert bars[T0 + 14 * MINUTE].wall_ratio.status is F.Status.OK


def test_wall_flag_needs_magnitude_not_just_a_p99_exceedance():
    """A bar holds ~1000 level observations, so by construction ~1% of them
    clear p99 and nearly every bar has some. The count is the feature; the
    flag needs a size on top of it or the gate fires all day.
    """
    con = make_con()
    wall_at = T0 + 15 * MINUTE
    build_wall_market(con, wall_at, wall_qty=40.0)        # only 4x the rest
    bars = {b.bar_start: b for b in
            build(con, wall_config(wall_flag_ratio=1.0)).bars(
                "TEST", T0 + 14 * MINUTE, T0 + 17 * MINUTE)}
    assert bars[wall_at].wall_fires > 0
    assert "wall" in bars[wall_at].flags(F.FeatureConfig(wall_flag_ratio=1.0))

    strict = build(con, wall_config(wall_flag_ratio=100.0))
    strict_bars = {b.bar_start: b for b in
                   strict.bars("TEST", T0 + 14 * MINUTE, T0 + 17 * MINUTE)}
    assert strict_bars[wall_at].wall_fires > 0            # still counted
    assert "wall" not in strict_bars[wall_at].flags(strict.config)


# --------------------------------------------------------------------------
# level lifetime and cancel/fill
# --------------------------------------------------------------------------

def level_config(**overrides):
    base = dict(level_min_usd=1_000.0, level_max_bps=50.0,
                level_fill_frac=0.5, level_exclude_estimated=True)
    base.update(overrides)
    return F.FeatureConfig(**base)


def resting_bid_market(con, present_from, present_to, qty=100.0, price=99.99,
                       step=100, span=10_000, est_window=None):
    """Snapshots every ``step`` ms; a big bid rests for part of the time."""
    for t in range(T0, T0 + span, step):
        bids = [(99.99, 1.0), (99.98, 1.0), (99.97, 1.0)]
        asks = [(100.01, 1.0), (100.02, 1.0)]
        if present_from <= t < present_to:
            bids = [(p, qty if p == price else q) for p, q in bids]
        est = bool(est_window and est_window[0] <= t < est_window[1])
        add_snapshot(con, t, bids, asks, est=est)


def test_level_vanishing_without_trades_is_a_cancellation():
    con = make_con()
    resting_bid_market(con, T0 + 2_000, T0 + 5_000)
    fx = build(con, level_config())
    eps = [e for e in fx.level_episodes("TEST", T0, T0 + 10_000) if e.max_usd > 1_000]
    assert len(eps) == 1
    ep = eps[0]
    assert ep.outcome is F.Outcome.CANCELLED
    assert ep.filled_qty == 0.0
    assert ep.lifetime_ms == 3_000


def test_level_consumed_by_aggressive_selling_is_a_fill():
    con = make_con()
    resting_bid_market(con, T0 + 2_000, T0 + 5_000)
    # An aggressive sell into the bid, just before it disappears.
    add_trade(con, T0 + 4_950, 99.99, 90.0, aggressive_buy=False)
    fx = build(con, level_config())
    eps = [e for e in fx.level_episodes("TEST", T0, T0 + 10_000) if e.max_usd > 1_000]
    assert len(eps) == 1
    assert eps[0].outcome is F.Outcome.FILLED
    assert eps[0].filled_qty == pytest.approx(90.0)


def test_aggressive_buying_does_not_fill_a_bid():
    """Only the opposite side can consume a resting bid. Counting both sides
    turns ordinary two-way trading into fills and hides real spoofing."""
    con = make_con()
    resting_bid_market(con, T0 + 2_000, T0 + 5_000)
    add_trade(con, T0 + 4_950, 99.99, 90.0, aggressive_buy=True)
    fx = build(con, level_config())
    eps = [e for e in fx.level_episodes("TEST", T0, T0 + 10_000) if e.max_usd > 1_000]
    assert eps[0].outcome is F.Outcome.CANCELLED


def test_level_leaving_the_visible_window_is_not_a_cancellation():
    """The feed only shows 20 levels. When price runs away, resting orders
    drop off the bottom of the window. Calling that a cancellation
    manufactures spoofing every time the market trends."""
    con = make_con()
    for k in range(20):
        t = T0 + k * 100
        bids = [(99.99, 500.0), (99.98, 1.0), (99.97, 1.0)]
        add_snapshot(con, t, bids, [(100.01, 1.0), (100.02, 1.0)])
    for k in range(20, 40):          # price jumps; 99.99 is no longer visible
        t = T0 + k * 100
        bids = [(105.00, 1.0), (104.99, 1.0)]
        add_snapshot(con, t, bids, [(105.02, 1.0), (105.03, 1.0)])

    fx = build(con, level_config())
    eps = [e for e in fx.level_episodes("TEST", T0, T0 + 10_000) if e.max_usd > 1_000]
    assert len(eps) == 1
    assert eps[0].outcome is F.Outcome.OUT_OF_BOOK


def test_level_still_resting_at_the_end_is_censored_not_long_lived():
    con = make_con()
    resting_bid_market(con, T0 + 2_000, T0 + 99_000)
    fx = build(con, level_config())
    eps = [e for e in fx.level_episodes("TEST", T0, T0 + 10_000) if e.max_usd > 1_000]
    assert eps[0].outcome is F.Outcome.OPEN


def test_reconstructed_timestamps_are_excluded_from_lifetimes():
    """est:1 stamps are ~22ms median / 67ms p90 off - fine for volume, not
    fine for a 300ms spoof lifetime.

    The level here rests only during the reconstructed stretch, so excluding
    those snapshots must make it vanish entirely rather than be timed badly.
    """
    con = make_con()
    resting_bid_market(con, T0 + 3_000, T0 + 4_000,
                       est_window=(T0 + 3_000, T0 + 4_000))

    excluded = build(con, level_config(level_exclude_estimated=True))
    included = build(con, level_config(level_exclude_estimated=False))

    n_ex = len(excluded.con.execute("SELECT * FROM book WHERE NOT est").fetchall())
    n_in = len(included.con.execute("SELECT * FROM book").fetchall())
    assert n_ex < n_in                       # the fixture really has est rows

    eps_ex = [e for e in excluded.level_episodes("TEST", T0, T0 + 10_000)
              if e.max_usd > 1_000]
    eps_in = [e for e in included.level_episodes("TEST", T0, T0 + 10_000)
              if e.max_usd > 1_000]
    assert len(eps_in) == 1
    assert eps_in[0].lifetime_ms == 1_000
    assert eps_ex == []
    assert excluded.level_stats("TEST", T0, T0 + 10_000).excluded_estimated


def test_level_stats_reports_a_bounded_rate():
    con = make_con()
    resting_bid_market(con, T0 + 2_000, T0 + 5_000)
    fx = build(con, level_config())
    stats = fx.level_stats("TEST", T0, T0 + 10_000)
    assert stats.cancel_rate.require() == pytest.approx(1.0)
    assert 0.0 <= stats.cancel_rate.require() <= 1.0
    assert stats.counts["cancelled"] >= 1
    assert stats.median_filled_ms is None


def test_level_stats_with_no_episodes_says_no_data():
    con = make_con()
    resting_bid_market(con, T0 + 99_000, T0 + 99_000)   # never present
    fx = build(con, level_config(level_min_usd=1e9))
    stats = fx.level_stats("TEST", T0, T0 + 10_000)
    assert stats.cancel_rate.status is F.Status.NO_DATA
    with pytest.raises(F.InsufficientHistory):
        stats.cancel_rate.require()


def test_level_episodes_refuses_an_unbounded_range():
    con = make_con()
    resting_bid_market(con, T0 + 2_000, T0 + 5_000)
    fx = build(con, level_config(level_max_snapshots=10))
    with pytest.raises(ValueError, match="level_max_snapshots"):
        fx.level_episodes("TEST", T0, T0 + 10_000)


# --------------------------------------------------------------------------
# contract
# --------------------------------------------------------------------------

def test_batch_extractor_satisfies_the_interface():
    assert issubclass(F.BatchFeatureExtractor, F.FeatureExtractor)
    for name in ("symbols", "coverage", "bars", "walls", "level_episodes"):
        assert getattr(F.BatchFeatureExtractor, name) is not getattr(
            F.FeatureExtractor, name), f"{name} not overridden"


def test_abstract_extractor_cannot_be_instantiated():
    with pytest.raises(TypeError):
        F.FeatureExtractor()


def test_coverage_and_symbols(quiet_market):
    fx = extractor(quiet_market)
    assert fx.symbols() == ["TEST"]
    first, last = fx.coverage("TEST")
    assert first == T0
    assert last < T0 + 120 * MINUTE
    assert fx.coverage("NOPE") is None


def test_bars_returns_nothing_for_an_empty_range(quiet_market):
    fx = extractor(quiet_market)
    assert fx.bars("TEST", T0 + 10 * MINUTE, T0 + 10 * MINUTE) == []


def test_every_feature_value_is_finite_or_none(quiet_market):
    fx = extractor(quiet_market)
    for b in fx.bars("TEST", T0 + 60 * MINUTE, T0 + 70 * MINUTE, include_walls=False):
        for w in (b.volume_z, b.thin_book, b.ofi, b.retracement, b.wall_ratio):
            assert w.value is None or math.isfinite(w.value)
