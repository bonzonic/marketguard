"""Rolling manipulation features, computed over the recorded archive.

Everything downstream - the statistical gate, both detectors, the auto-labeller
- consumes this module, so the decisions here are mostly about *not lying*.

**Robust statistics, not mean/stdev.** A trailing window wide enough to be a
useful baseline is also wide enough to contain a previous pump. Standard
deviation is dominated by exactly those outliers, so the window inflates its
own threshold and the next pump scores an unremarkable z. Median and MAD do
not move when a few observations go to the moon. The z reported here is the
raw ``(x - median) / MAD`` the thresholds were written against, *not* the
0.6745-scaled "modified z-score" - scaling would silently move the stated
5/10 cut points by a factor of 1.48.

**Missing history is a state, not a number.** Only ~3 days of capture exist
and there are hour-long holes inside it, so a trailing window will routinely
be short. Returning 0, or None that flows onward, is how a surveillance system
ends up confidently reporting nothing. Every windowed value here carries a
:class:`Status`; a short-but-usable window is ``PARTIAL_HISTORY`` (value
present, visibly degraded) and a too-short one is ``INSUFFICIENT_HISTORY``
(value ``None``). :meth:`Windowed.require` raises rather than let a caller
read through a missing value by accident.

**An uncovered bar is not a zero-volume bar.** The recorder stopped and
restarted during the capture. Left-joining a dense minute grid onto the trades
table turns every un-recorded minute into a 0.0, and a window half-full of
fabricated zeros drives the median to 0, the MAD to 0, and the z-score to
nonsense. A bar counts as covered only if it saw a book snapshot or a trade;
uncovered bars are excluded from every trailing reference and reported
``NO_DATA``.

**One clock.** Trades carry both the exchange event time ``E`` and the
recorder's receive stamp ``t``; depth snapshots carry only ``t``. Measured on
this archive the two differ by a median of 742ms, so binning trades by ``E``
and book by ``t`` shears the two series against each other. Default is ``t``
for both - the only clock both streams actually share. Set
``FeatureConfig.trade_clock = "event"`` if you need to line up against
something external.

**Batch only, for now.** The historical path over Parquet is what the
auto-labeller and threshold tuning need, and it is the only path that can be
validated against real capture. :class:`FeatureExtractor` is the contract a
future streaming implementation must satisfy: same config, same row
dataclasses, same pure statistics - a live version keeps ``deque``s of bars
and calls :func:`robust_z` / :func:`percentile_rank` / :func:`ofi` on them
rather than asking DuckDB for the trailing window.

Usage:
    python features.py [symbol] [hours]
"""
from __future__ import annotations

import os
import statistics
import sys
from abc import ABC, abstractmethod
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence

import duckdb

import config
import query

MS_PER_MINUTE = 60_000


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

class Status(str, Enum):
    """Why a windowed value is, or is not, trustworthy.

    ``str`` mixin so these serialise straight into the feature summaries the
    LLM cascade receives without a custom encoder.
    """

    OK = "ok"
    PARTIAL_HISTORY = "partial_history"        # computed from a short window
    INSUFFICIENT_HISTORY = "insufficient_history"
    NO_DATA = "no_data"                        # nothing recorded for this bar
    ZERO_SCALE = "zero_scale"                  # dispersion is 0; z undefined
    ZERO_SCALE_FALLBACK = "zero_scale_fallback"  # MAD was 0, mean-AD used
    NO_PUMP = "no_pump"                        # retracement needs a run-up


_USABLE = (Status.OK, Status.PARTIAL_HISTORY, Status.ZERO_SCALE_FALLBACK)


class InsufficientHistory(Exception):
    """Raised by :meth:`Windowed.require` when a value was never computed."""


@dataclass(frozen=True)
class Windowed:
    """A rolling feature value together with what it was computed from.

    ``value`` is ``None`` whenever ``status`` is not usable. Read it through
    :meth:`require` in code that cannot cope with a gap; read ``.value`` only
    after checking ``.ok``.
    """

    value: float | None
    status: Status
    n_obs: int = 0
    n_required: int = 0

    @property
    def ok(self) -> bool:
        return self.status in _USABLE and self.value is not None

    def require(self) -> float:
        if not self.ok:
            raise InsufficientHistory(
                f"{self.status.value}: {self.n_obs} of {self.n_required} observations"
            )
        assert self.value is not None
        return self.value

    def __repr__(self) -> str:  # compact enough to print a whole bar
        if self.value is None:
            return f"<{self.status.value} {self.n_obs}/{self.n_required}>"
        return f"{self.value:.3f}[{self.status.value}]"


def _history_status(n_obs: int, n_required: int, min_frac: float) -> Status:
    """OK / PARTIAL_HISTORY / INSUFFICIENT_HISTORY for a trailing window."""
    if n_required <= 0:
        return Status.OK
    if n_obs >= n_required:
        return Status.OK
    if n_obs >= max(2, int(round(min_frac * n_required))):
        return Status.PARTIAL_HISTORY
    return Status.INSUFFICIENT_HISTORY


_SEVERITY = {
    Status.OK: 0,
    Status.ZERO_SCALE_FALLBACK: 1,
    Status.PARTIAL_HISTORY: 2,
    Status.NO_PUMP: 3,
    Status.ZERO_SCALE: 4,
    Status.INSUFFICIENT_HISTORY: 5,
    Status.NO_DATA: 6,
}


def worst(*statuses: Status) -> Status:
    """The least trustworthy of several statuses.

    A feature that needs two windows (retracement needs a pump window *and* a
    baseline window) is only as good as the weaker one.
    """
    return max(statuses, key=lambda s: _SEVERITY[s])


# --------------------------------------------------------------------------
# pure statistics
#
# These are deliberately dependency-free and deliberately small: getting
# median/MAD, percentile rank or the OFI sign wrong produces confident
# nonsense that still runs, so they are the part of this module that gets
# real unit tests. No numpy is available anyway.
# --------------------------------------------------------------------------

def median(values: Sequence[float]) -> float:
    """Median. Raises on an empty sequence rather than inventing a value."""
    if not values:
        raise ValueError("median of empty sequence")
    return statistics.median(values)


def mad(values: Sequence[float], center: float | None = None) -> float:
    """Median absolute deviation - the dispersion a few pumps cannot move."""
    if not values:
        raise ValueError("mad of empty sequence")
    mid = median(values) if center is None else center
    return statistics.median([abs(v - mid) for v in values])


def nonzero_mad(values: Sequence[float], center: float | None = None) -> float:
    """Median of the *strictly positive* absolute deviations, or 0.

    Only used when MAD itself is exactly 0, which happens whenever more than
    half a window's bars are identical - typically a run of zero-volume
    minutes in an illiquid symbol. That is precisely the situation before a
    pump, so refusing to score it would blind the detector at the worst
    moment.

    Note what this is *not*: the mean absolute deviation. Mean-AD is also
    non-zero here, and it is what one reaches for first, but it is dominated
    by large observations - so a window of [10]*20 plus three past pumps at
    ~10,000 gives it a scale of ~1,300 and it reintroduces exactly the
    masking that MAD exists to prevent. Taking a median over the non-zero
    deviations keeps the fallback robust.
    """
    if not values:
        raise ValueError("nonzero_mad of empty sequence")
    mid = median(values) if center is None else center
    deviations = [abs(v - mid) for v in values if v != mid]
    return statistics.median(deviations) if deviations else 0.0


def robust_z(value: float, reference: Sequence[float]) -> tuple[float | None, Status]:
    """``(value - median) / MAD`` over ``reference``, with a scale fallback.

    Returns ``(z, status)``. ``ZERO_SCALE_FALLBACK`` means MAD was 0 and
    :func:`nonzero_mad` was substituted - still a robust scale, but a
    different one, so the threshold the caller applies is not quite the
    threshold it was tuned for and it must be able to see that.
    ``ZERO_SCALE`` means every reference observation was identical and no
    scale exists at all.
    """
    mid = median(reference)
    scale = mad(reference, mid)
    if scale > 0:
        return (value - mid) / scale, Status.OK
    scale = nonzero_mad(reference, mid)
    if scale > 0:
        return (value - mid) / scale, Status.ZERO_SCALE_FALLBACK
    return None, Status.ZERO_SCALE


def percentile_rank(value: float, reference: Sequence[float]) -> float:
    """Fraction of ``reference`` below ``value``, in [0, 1].

    Ties count a half each (the mid-rank convention). With ``<=`` a window of
    identical depths would rank every observation at 1.0 and the thin-book
    test would never fire; with ``<`` it would rank them all at 0.0 and it
    would fire constantly. Mid-rank puts them at 0.5, which is the honest
    answer for "this is exactly typical".
    """
    if not reference:
        raise ValueError("percentile_rank of empty reference")
    ordered = sorted(reference)
    lo = bisect_left(ordered, value)
    hi = bisect_right(ordered, value)
    return (lo + (hi - lo) / 2.0) / len(ordered)


def quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile (the same definition DuckDB's
    ``quantile_cont`` and numpy's default use), so Python-side and SQL-side
    thresholds agree."""
    if not values:
        raise ValueError("quantile of empty sequence")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"quantile out of range: {q}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def ofi(buy_volume: float, sell_volume: float) -> float | None:
    """Order flow imbalance in [-1, +1]; +1 is entirely aggressive buying.

    ``buy_volume`` is volume where the *taker* bought - i.e. Binance's
    ``m: false``, the buyer crossing the spread. The field reads backwards
    ("was the buyer the maker"), and swapping it flips the sign of this
    feature while every downstream consumer keeps running happily. etl.py
    resolves it once into ``trades.aggressive_buy``; nothing else should touch
    ``is_buyer_maker`` directly.
    """
    total = buy_volume + sell_volume
    if total <= 0:
        return None
    return (buy_volume - sell_volume) / total


def retracement(
    highs: Sequence[float | None],
    lows: Sequence[float | None],
    baseline: float,
) -> tuple[float | None, float | None, float | None]:
    """``(peak - trough) / (peak - baseline)`` over a pump window.

    Returns ``(ratio, peak, trough)``. ``highs``/``lows`` are in time order
    and may contain ``None`` for un-recorded bars. The trough is searched
    only *after* the peak - a low that precedes the run-up is not a
    retracement of it, and ignoring the ordering turns every ordinary
    oscillation into a confirmed dump.

    ``None`` ratio means there was no run-up above the baseline to retrace.
    Values above 1.0 are real and are not clamped: they mean the fall carried
    price below where it started, which is the ordinary outcome of a dump.
    """
    pairs = [(h, lo) for h, lo in zip(highs, lows) if h is not None and lo is not None]
    if not pairs:
        return None, None, None
    peak_idx = max(range(len(pairs)), key=lambda i: pairs[i][0])
    peak = pairs[peak_idx][0]
    trough = min(lo for _, lo in pairs[peak_idx:])
    if peak <= baseline:
        return None, peak, trough
    return (peak - trough) / (peak - baseline), peak, trough


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FeatureConfig:
    """Every window is configurable because only ~3 days of capture exist.

    The defaults are sized to fit *this* archive - a one-hour trailing
    reference, not the one-week reference the design assumed. Widen them once
    more data exists; nothing here breaks, the statuses just turn into
    ``PARTIAL_HISTORY`` and then ``INSUFFICIENT_HISTORY`` as you outrun the
    capture.
    """

    bar_ms: int = MS_PER_MINUTE

    # --- volume z-score -------------------------------------------------
    volume_ref_bars: int = 60
    volume_notable_z: float = 5.0
    volume_extreme_z: float = 10.0

    # --- thin book ------------------------------------------------------
    # +/-1% of mid. Note that on this capture the @depth20 window spans only
    # 16-152bps end to end, so for most symbols this band already covers the
    # entire visible book and the filter rarely binds. It still matters for
    # the wider-tick symbols (opusdt, arbusdt) and for any future full-depth
    # feed, so the clamp stays.
    depth_band_pct: float = 0.01
    depth_ref_bars: int = 60
    thin_book_pctile: float = 0.20

    # --- order flow imbalance -------------------------------------------
    ofi_window_bars: int = 5

    # --- retracement ----------------------------------------------------
    pump_window_bars: int = 30
    baseline_bars: int = 30
    retracement_fires_at: float = 0.70
    # Below this run-up the ratio is dividing one piece of noise by another.
    # Worth knowing before tuning: across 51h of solusdt the largest run-up in
    # any 30-minute window was 1.74% and the median was 0.74%, so this archive
    # contains no pump of the size the detector is built for. Every
    # retracement > 0.7 in it is noise retracing noise. Raise this once a real
    # pump has been captured; leaving it low keeps the feature exercised.
    min_runup_pct: float = 0.005

    # --- walls ----------------------------------------------------------
    # Walls are evaluated on a 1/second grid rather than every snapshot: the
    # book is pushed at 100ms and resting size is enormously autocorrelated,
    # so all ten snapshots in a second say the same thing at ten times the
    # cost. Anything that lives less than a second is a spoof lifetime
    # question, which level_episodes() answers instead.
    wall_sample_ms: int = 1_000
    wall_band_bps: float = 5.0
    wall_max_bps: float = 50.0
    wall_proximity_bps: float = 20.0
    wall_quantile: float = 0.99
    wall_ref_ms: int = 6 * 3_600_000
    wall_min_ref_obs: int = 2_000
    # "Above p99" is a criterion on a single level observation, and a one
    # minute bar contains around a thousand of them, so by construction ~1%
    # of them clear the bar and almost every minute has a few. Measured on
    # 51h of solusdt: 80% of bars contain at least one p99 exceedance, median
    # 14 per bar. That is the feature behaving correctly and the *flag* being
    # useless, so flagging additionally requires a magnitude. At 3x the band
    # p99 the same 51h flags 5.3% of bars, which is a gate a detector can
    # actually afford. wall_fires still counts plain p99 exceedances.
    wall_flag_ratio: float = 3.0

    # --- level lifetime / cancel-fill ------------------------------------
    level_max_bps: float = 20.0
    # Absolute, and therefore symbol-specific: $50k of resting size is a
    # routine level on solusdt (median depth within 1% is ~$3.1M) and a
    # once-an-hour event on avaxusdt (~$192k). Measured over one hour that is
    # 16,443 episodes against 3. Set it per symbol - a fraction of that
    # symbol's median near-mid depth is the obvious rule - rather than
    # trusting this default across a watchlist.
    level_min_usd: float = 50_000.0
    level_fill_frac: float = 0.5
    # est:1 timestamps are ~22ms median / 67ms p90 off. Fine for volume and
    # pump windows, not fine for sub-100ms lifetimes, so lifetimes exclude
    # them by default.
    level_exclude_estimated: bool = True
    level_max_snapshots: int = 1_500_000

    # --- degradation ------------------------------------------------------
    # A window at least this full is still reported, flagged PARTIAL_HISTORY.
    min_ref_frac: float = 0.5

    trade_clock: str = "receive"  # "receive" (t) or "event" (E)

    def __post_init__(self) -> None:
        if self.trade_clock not in ("receive", "event"):
            raise ValueError(f"trade_clock must be receive|event, got {self.trade_clock!r}")
        for name in ("bar_ms", "volume_ref_bars", "depth_ref_bars",
                     "ofi_window_bars", "pump_window_bars", "baseline_bars"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")

    @property
    def trade_clock_column(self) -> str:
        return "t" if self.trade_clock == "receive" else "event_time"

    @property
    def lookback_bars(self) -> int:
        """Bars of history needed before the first requested bar."""
        return max(
            self.volume_ref_bars,
            self.depth_ref_bars,
            self.ofi_window_bars - 1,
            self.pump_window_bars + self.baseline_bars,
        )

    @property
    def lookback_ms(self) -> int:
        return self.lookback_bars * self.bar_ms


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BarFeatures:
    """One symbol, one bar, every rolling feature plus its provenance."""

    symbol: str
    bar_start: int
    bar_ms: int

    covered: bool
    n_trades: int
    n_snapshots: int

    volume_base: float
    volume_usd: float
    buy_usd: float
    sell_usd: float

    depth_usd: float | None
    bid_depth_usd: float | None
    ask_depth_usd: float | None
    high: float | None
    low: float | None
    close: float | None

    volume_z: Windowed
    thin_book: Windowed          # percentile rank of depth in [0, 1]
    ofi: Windowed
    retracement: Windowed

    wall_fires: int = 0
    wall_max_usd: float | None = None
    wall_nearest_bps: float | None = None
    wall_ratio: Windowed = field(
        default_factory=lambda: Windowed(None, Status.NO_DATA)
    )

    # Filled in alongside retracement so a detector can gate on run-up size.
    runup_pct: float | None = None
    pump_peak: float | None = None
    pump_trough: float | None = None

    @property
    def bar_end(self) -> int:
        return self.bar_start + self.bar_ms

    def flags(self, cfg: FeatureConfig) -> tuple[str, ...]:
        """Signal names that fired on this bar, for the statistical gate."""
        out: list[str] = []
        if self.volume_z.ok:
            z = self.volume_z.require()
            if z > cfg.volume_extreme_z:
                out.append("volume_extreme")
            elif z > cfg.volume_notable_z:
                out.append("volume_notable")
        if self.thin_book.ok and self.thin_book.require() < cfg.thin_book_pctile:
            out.append("thin_book")
        if (self.wall_fires > 0 and self.wall_ratio.ok
                and self.wall_ratio.require() >= cfg.wall_flag_ratio):
            out.append("wall")
        if self.retracement.ok and self.retracement.require() > cfg.retracement_fires_at:
            out.append("retracement")
        return tuple(out)


@dataclass(frozen=True)
class WallEvent:
    """A single resting level that stayed above its band's p99, merged across
    the consecutive samples that observed it."""

    symbol: str
    side: str            # "bid" | "ask"
    price: float
    first_t: int
    last_t: int
    n_samples: int
    max_usd: float
    max_ratio: float     # size / trailing p99 for its price band
    min_bps: float       # closest it came to mid
    ref_n: int           # observations behind the p99 estimate

    @property
    def duration_ms(self) -> int:
        return self.last_t - self.first_t


class Outcome(str, Enum):
    """How a resting level stopped resting."""

    CANCELLED = "cancelled"
    FILLED = "filled"
    PARTIAL = "partial"
    # The book moved and the level fell out of the 20-level window. We cannot
    # tell what happened to it and must not count it as a cancellation - doing
    # so would manufacture spoofing every time price trends.
    OUT_OF_BOOK = "out_of_book"
    # Still resting when the range ended: right-censored, not long-lived.
    OPEN = "open"


@dataclass(frozen=True)
class LevelEpisode:
    """A span during which >= ``level_min_usd`` rested at one price."""

    symbol: str
    side: str
    price: float
    first_t: int
    last_t: int
    max_usd: float
    last_usd: float
    filled_qty: float
    outcome: Outcome

    @property
    def lifetime_ms(self) -> int:
        return self.last_t - self.first_t


@dataclass(frozen=True)
class LevelStats:
    """Aggregate of :class:`LevelEpisode`, as the detectors want it."""

    symbol: str
    n_episodes: int
    counts: dict[str, int]
    cancel_rate: Windowed          # cancelled / (cancelled + filled)
    median_cancelled_ms: float | None
    median_filled_ms: float | None
    p90_cancelled_ms: float | None
    excluded_estimated: bool


# --------------------------------------------------------------------------
# contract
# --------------------------------------------------------------------------

class FeatureExtractor(ABC):
    """What a feature source must provide, batch or live.

    The batch implementation asks DuckDB for the trailing window; a live one
    would keep bounded ``deque``s and call the same pure functions. Both must
    return the same dataclasses with the same statuses, so the gate and the
    auto-labeller cannot tell them apart.
    """

    config: FeatureConfig

    @abstractmethod
    def symbols(self) -> list[str]:
        """Symbols this source can produce features for."""

    @abstractmethod
    def coverage(self, symbol: str) -> tuple[int, int] | None:
        """``(first_ms, last_ms)`` of available data, or None."""

    @abstractmethod
    def bars(self, symbol: str, start: int, end: int,
             include_walls: bool = True) -> list[BarFeatures]:
        """Bar-grain features for ``[start, end)``.

        History before ``start`` is used for the trailing windows but is not
        returned. Bars the recorder never covered are returned with
        ``covered=False`` rather than omitted, so a consumer sees the hole.
        """

    @abstractmethod
    def walls(self, symbol: str, start: int, end: int) -> list[WallEvent]:
        """Individual wall episodes in ``[start, end)``."""

    @abstractmethod
    def level_episodes(self, symbol: str, start: int, end: int) -> list[LevelEpisode]:
        """Resting-level episodes in ``[start, end)``, with an outcome each."""

    def level_stats(self, symbol: str, start: int, end: int) -> LevelStats:
        """Cancel/fill summary over :meth:`level_episodes`.

        Reported as a *rate* rather than the raw cancel/fill ratio: with no
        fills at all the ratio is infinite, and an unbounded feature is a
        threshold that cannot be tuned.
        """
        episodes = self.level_episodes(symbol, start, end)
        counts: dict[str, int] = {o.value: 0 for o in Outcome}
        for ep in episodes:
            counts[ep.outcome.value] += 1

        cancelled = [ep.lifetime_ms for ep in episodes if ep.outcome is Outcome.CANCELLED]
        filled = [ep.lifetime_ms for ep in episodes
                  if ep.outcome in (Outcome.FILLED, Outcome.PARTIAL)]
        decided = len(cancelled) + len(filled)
        if decided == 0:
            rate = Windowed(None, Status.NO_DATA, 0, 1)
        else:
            rate = Windowed(len(cancelled) / decided, Status.OK, decided, decided)

        return LevelStats(
            symbol=symbol,
            n_episodes=len(episodes),
            counts=counts,
            cancel_rate=rate,
            median_cancelled_ms=statistics.median(cancelled) if cancelled else None,
            median_filled_ms=statistics.median(filled) if filled else None,
            p90_cancelled_ms=quantile(cancelled, 0.9) if cancelled else None,
            excluded_estimated=self.config.level_exclude_estimated,
        )


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------

_BARS_SQL = """
WITH grid AS (
    SELECT * FROM generate_series(?, ?, {bar_ms}) AS g(bar_start)
),
tb AS (
    SELECT ({clock} // {bar_ms}) * {bar_ms} AS bar_start,
           count(*)                                                   AS n_trades,
           sum(qty)                                                   AS volume_base,
           sum(qty * price)                                           AS volume_usd,
           sum(CASE WHEN aggressive_buy THEN qty * price ELSE 0 END)  AS buy_usd,
           sum(CASE WHEN aggressive_buy THEN 0 ELSE qty * price END)  AS sell_usd
    FROM trades
    WHERE symbol = ? AND {clock} >= ? AND {clock} < ?
    GROUP BY 1
),
snap AS (
    SELECT t, mid,
           list_sum(list_transform(
               list_filter(bids, x -> x.price >= mid * (1 - {pct})),
               x -> x.price * x.qty))                                 AS bid_usd,
           list_sum(list_transform(
               list_filter(asks, x -> x.price <= mid * (1 + {pct})),
               x -> x.price * x.qty))                                 AS ask_usd
    FROM book
    WHERE symbol = ? AND t >= ? AND t < ?
),
bb AS (
    SELECT (t // {bar_ms}) * {bar_ms}                AS bar_start,
           count(*)                                  AS n_snapshots,
           quantile_cont(bid_usd + ask_usd, 0.5)     AS depth_usd,
           quantile_cont(bid_usd, 0.5)               AS bid_depth_usd,
           quantile_cont(ask_usd, 0.5)               AS ask_depth_usd,
           max(mid)                                  AS high,
           min(mid)                                  AS low,
           last(mid ORDER BY t)                      AS close
    FROM snap
    GROUP BY 1
),
j AS (
    SELECT g.bar_start,
           coalesce(tb.n_trades, 0)      AS n_trades,
           coalesce(tb.volume_base, 0.0) AS volume_base,
           coalesce(tb.volume_usd, 0.0)  AS volume_usd,
           coalesce(tb.buy_usd, 0.0)     AS buy_usd,
           coalesce(tb.sell_usd, 0.0)    AS sell_usd,
           coalesce(bb.n_snapshots, 0)   AS n_snapshots,
           bb.depth_usd, bb.bid_depth_usd, bb.ask_depth_usd,
           bb.high, bb.low, bb.close,
           (coalesce(bb.n_snapshots, 0) > 0 OR coalesce(tb.n_trades, 0) > 0) AS covered
    FROM grid g
    LEFT JOIN tb USING (bar_start)
    LEFT JOIN bb USING (bar_start)
)
SELECT bar_start, covered, n_trades, n_snapshots,
       volume_base, volume_usd, buy_usd, sell_usd,
       depth_usd, bid_depth_usd, ask_depth_usd, high, low, close,
       -- Uncovered bars are dropped from every trailing reference: a minute
       -- the recorder never saw is not a minute with no volume.
       list_filter(list(CASE WHEN covered THEN volume_usd END) OVER vol_w,
                   v -> v IS NOT NULL)                       AS volume_ref,
       list_filter(list(depth_usd) OVER depth_w,
                   v -> v IS NOT NULL)                       AS depth_ref,
       sum(CASE WHEN covered THEN buy_usd ELSE 0 END)  OVER ofi_w AS ofi_buy_usd,
       sum(CASE WHEN covered THEN sell_usd ELSE 0 END) OVER ofi_w AS ofi_sell_usd,
       sum(CASE WHEN covered THEN 1 ELSE 0 END)        OVER ofi_w AS ofi_bars,
       list(high)  OVER pump_w                               AS pump_highs,
       list(low)   OVER pump_w                               AS pump_lows,
       list_filter(list(close) OVER base_w, v -> v IS NOT NULL) AS baseline_closes
FROM j
WINDOW
    vol_w   AS (ORDER BY bar_start ROWS BETWEEN {vol_ref} PRECEDING AND 1 PRECEDING),
    depth_w AS (ORDER BY bar_start ROWS BETWEEN {depth_ref} PRECEDING AND 1 PRECEDING),
    ofi_w   AS (ORDER BY bar_start ROWS BETWEEN {ofi_prev} PRECEDING AND CURRENT ROW),
    pump_w  AS (ORDER BY bar_start ROWS BETWEEN {pump_prev} PRECEDING AND CURRENT ROW),
    base_w  AS (ORDER BY bar_start ROWS BETWEEN {base_prev} PRECEDING AND {pump} PRECEDING)
ORDER BY bar_start
"""


# The p99 reference is a trailing window of *level observations inside the same
# price band*. Comparing a level against the whole book instead would make
# every top-of-book level look ordinary next to the deep ones, and every deep
# level look like a wall. Note the band index uses floor(), not DuckDB's `//`:
# `//` on DOUBLE operands is ordinary division, so `bps // 5` silently yields a
# distinct float "band" per level and every band ends up with one observation.
_WALL_REF_CTE = """
WITH grid AS (
    SELECT t, mid, bids, asks
    FROM book
    WHERE symbol = ? AND t >= ? AND t < ?
    QUALIFY row_number() OVER (PARTITION BY t // {sample_ms} ORDER BY t DESC) = 1
),
lv AS (
    SELECT t, 'bid' AS side, b.price AS price, b.price * b.qty AS usd,
           (mid - b.price) / mid * 10000 AS bps
    FROM grid, UNNEST(bids) AS _(b)
    WHERE (mid - b.price) / mid * 10000 < {max_bps}
    UNION ALL
    SELECT t, 'ask', a.price, a.price * a.qty,
           (a.price - mid) / mid * 10000
    FROM grid, UNNEST(asks) AS _(a)
    WHERE (a.price - mid) / mid * 10000 < {max_bps}
),
banded AS (
    SELECT *, cast(floor(bps / {band_bps}) AS INTEGER) AS band FROM lv
),
ref AS (
    SELECT *,
           quantile_cont(usd, {q}) OVER w AS p99,
           count(*)                OVER w AS ref_n
    FROM banded
    WINDOW w AS (PARTITION BY side, band ORDER BY t
                 RANGE BETWEEN {ref_ms} PRECEDING AND 1 PRECEDING)
),
near AS (
    SELECT t, side, price, usd, bps, p99, usd / p99 AS ratio, ref_n
    FROM ref
    WHERE t >= ? AND bps <= {prox_bps} AND p99 IS NOT NULL AND p99 > 0
)
"""

_WALL_FIRES_SQL = _WALL_REF_CTE + """
SELECT t, side, price, usd, bps, p99, ratio, ref_n
FROM near
WHERE ratio > 1 AND ref_n >= {min_ref}
ORDER BY t
"""

# One pass, one row per bar: both the fire aggregate and max(ref_n), so a bar
# whose per-band reference was never populated is reported as
# INSUFFICIENT_HISTORY rather than as a quiet "no walls here".
_WALL_BARS_SQL = _WALL_REF_CTE + """
SELECT (t // {bar_ms}) * {bar_ms}                       AS bar_start,
       count(*) FILTER (WHERE ratio > 1)                AS n_fires,
       max(ratio)                                       AS max_ratio,
       max(usd)                                         AS max_usd,
       min(bps) FILTER (WHERE ratio > 1)                AS nearest_bps,
       max(ref_n)                                       AS ref_n
FROM near
GROUP BY 1
ORDER BY 1
"""


_SNAPSHOT_SQL = """
SELECT t, mid, est,
       list_min(list_transform(bids, x -> x.price)) AS bid_floor,
       list_max(list_transform(asks, x -> x.price)) AS ask_ceil,
       list_filter(bids, x -> x.price * x.qty >= {min_usd}
                          AND (mid - x.price) / mid * 10000 <= {max_bps}) AS big_bids,
       list_filter(asks, x -> x.price * x.qty >= {min_usd}
                          AND (x.price - mid) / mid * 10000 <= {max_bps}) AS big_asks
FROM book
WHERE symbol = ? AND t >= ? AND t < ? {est_filter}
ORDER BY t
"""


# --------------------------------------------------------------------------
# batch implementation
# --------------------------------------------------------------------------

class BatchFeatureExtractor(FeatureExtractor):
    """Historical features over Parquet, via DuckDB.

    The split of work is deliberate: DuckDB does the row-crushing (binning
    millions of snapshots into bars, unnesting levels, sliding the per-band
    p99) and hands Python a trailing *list* per bar; Python then applies the
    unit-tested statistics in this module. That way the median/MAD/percentile
    code that ships is the same code the tests exercise, instead of a second
    implementation living in SQL where nothing checks it.
    """

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection | None = None,
        parquet_dir: Path | None = None,
        cfg: FeatureConfig | None = None,
    ) -> None:
        self.config = cfg or FeatureConfig()
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

    # -- bars ------------------------------------------------------------

    def bars(self, symbol: str, start: int, end: int,
             include_walls: bool = True) -> list[BarFeatures]:
        cfg = self.config
        first_bar = _floor_bar(start, cfg.bar_ms)
        last_bar = _floor_bar(end - 1, cfg.bar_ms)
        if last_bar < first_bar:
            return []
        hist_bar = first_bar - cfg.lookback_ms

        sql = _BARS_SQL.format(
            bar_ms=cfg.bar_ms,
            clock=cfg.trade_clock_column,
            pct=cfg.depth_band_pct,
            vol_ref=cfg.volume_ref_bars,
            depth_ref=cfg.depth_ref_bars,
            ofi_prev=cfg.ofi_window_bars - 1,
            pump_prev=cfg.pump_window_bars - 1,
            pump=cfg.pump_window_bars,
            base_prev=cfg.pump_window_bars + cfg.baseline_bars - 1,
        )
        window_end = last_bar + cfg.bar_ms
        rows = self.con.execute(
            sql,
            [hist_bar, last_bar,
             symbol, hist_bar, window_end,
             symbol, hist_bar, window_end],
        ).fetchall()
        columns = [d[0] for d in self.con.description]

        wall_bars: dict[int, dict] = {}
        if include_walls:
            wall_bars = self._wall_bars(symbol, first_bar, window_end)

        out: list[BarFeatures] = []
        for row in rows:
            r = dict(zip(columns, row))
            if r["bar_start"] < first_bar:
                continue  # history, used for the trailing windows only
            out.append(self._build_bar(symbol, r, wall_bars.get(r["bar_start"])))
        return out

    def _build_bar(self, symbol: str, r: dict, wall: dict | None) -> BarFeatures:
        cfg = self.config
        covered = bool(r["covered"])

        # --- volume z ---
        vol_ref = r["volume_ref"] or []
        if not covered:
            volume_z = Windowed(None, Status.NO_DATA, len(vol_ref), cfg.volume_ref_bars)
        else:
            status = _history_status(len(vol_ref), cfg.volume_ref_bars, cfg.min_ref_frac)
            if status is Status.INSUFFICIENT_HISTORY:
                volume_z = Windowed(None, status, len(vol_ref), cfg.volume_ref_bars)
            else:
                z, zstatus = robust_z(r["volume_usd"], vol_ref)
                if zstatus is not Status.OK:
                    status = zstatus
                volume_z = Windowed(z, status, len(vol_ref), cfg.volume_ref_bars)

        # --- thin book ---
        depth_ref = r["depth_ref"] or []
        if r["depth_usd"] is None:
            thin = Windowed(None, Status.NO_DATA, len(depth_ref), cfg.depth_ref_bars)
        else:
            status = _history_status(len(depth_ref), cfg.depth_ref_bars, cfg.min_ref_frac)
            if status is Status.INSUFFICIENT_HISTORY:
                thin = Windowed(None, status, len(depth_ref), cfg.depth_ref_bars)
            else:
                thin = Windowed(percentile_rank(r["depth_usd"], depth_ref), status,
                                len(depth_ref), cfg.depth_ref_bars)

        # --- order flow imbalance ---
        ofi_bars = int(r["ofi_bars"] or 0)
        imbalance = ofi(r["ofi_buy_usd"] or 0.0, r["ofi_sell_usd"] or 0.0)
        if imbalance is None:
            ofi_w = Windowed(None, Status.NO_DATA, ofi_bars, cfg.ofi_window_bars)
        else:
            ofi_w = Windowed(imbalance,
                             _history_status(ofi_bars, cfg.ofi_window_bars, cfg.min_ref_frac),
                             ofi_bars, cfg.ofi_window_bars)

        # --- retracement ---
        baseline_closes = r["baseline_closes"] or []
        highs = r["pump_highs"] or []
        lows = r["pump_lows"] or []
        n_pump = sum(1 for h in highs if h is not None)
        required = cfg.pump_window_bars
        ratio = peak = trough = runup = None
        rstatus = worst(
            _history_status(n_pump, cfg.pump_window_bars, cfg.min_ref_frac),
            _history_status(len(baseline_closes), cfg.baseline_bars, cfg.min_ref_frac),
        )
        if rstatus is not Status.INSUFFICIENT_HISTORY:
            baseline = median(baseline_closes)
            ratio, peak, trough = retracement(highs, lows, baseline)
            if peak is not None and baseline > 0:
                runup = peak / baseline - 1.0
            if ratio is None or runup is None or runup < cfg.min_runup_pct:
                ratio = None
                rstatus = Status.NO_PUMP
        retr = Windowed(ratio, rstatus, n_pump, required)

        # --- walls ---
        # `ratio` here is the biggest near-mid level as a multiple of its own
        # band's trailing p99, so it is defined even on quiet bars: <= 1 means
        # nothing in the book stood out, > 1 is a fire.
        if wall is None:
            wall_fires, wall_max_usd, wall_nearest = 0, None, None
            wall_ratio = Windowed(None, Status.NO_DATA, 0, cfg.wall_min_ref_obs)
        else:
            ref_n = int(wall["ref_n"] or 0)
            wall_fires = int(wall["n_fires"] or 0)
            wall_max_usd = wall["max_usd"]
            wall_nearest = wall["nearest_bps"]
            if ref_n < cfg.wall_min_ref_obs:
                wall_ratio = Windowed(None, Status.INSUFFICIENT_HISTORY,
                                      ref_n, cfg.wall_min_ref_obs)
                wall_fires = 0
            else:
                wall_ratio = Windowed(wall["max_ratio"], Status.OK,
                                      ref_n, cfg.wall_min_ref_obs)

        return BarFeatures(
            symbol=symbol,
            bar_start=int(r["bar_start"]),
            bar_ms=cfg.bar_ms,
            covered=covered,
            n_trades=int(r["n_trades"]),
            n_snapshots=int(r["n_snapshots"]),
            volume_base=float(r["volume_base"]),
            volume_usd=float(r["volume_usd"]),
            buy_usd=float(r["buy_usd"]),
            sell_usd=float(r["sell_usd"]),
            depth_usd=r["depth_usd"],
            bid_depth_usd=r["bid_depth_usd"],
            ask_depth_usd=r["ask_depth_usd"],
            high=r["high"], low=r["low"], close=r["close"],
            volume_z=volume_z,
            thin_book=thin,
            ofi=ofi_w,
            retracement=retr,
            wall_fires=wall_fires,
            wall_max_usd=wall_max_usd,
            wall_nearest_bps=wall_nearest,
            wall_ratio=wall_ratio,
            runup_pct=runup,
            pump_peak=peak,
            pump_trough=trough,
        )

    # -- walls -----------------------------------------------------------

    def _wall_sql(self, template: str) -> str:
        cfg = self.config
        return template.format(
            sample_ms=cfg.wall_sample_ms,
            max_bps=cfg.wall_max_bps,
            band_bps=cfg.wall_band_bps,
            q=cfg.wall_quantile,
            ref_ms=cfg.wall_ref_ms,
            prox_bps=cfg.wall_proximity_bps,
            min_ref=cfg.wall_min_ref_obs,
            bar_ms=cfg.bar_ms,
        )

    def _wall_params(self, start: int, end: int, symbol: str) -> list:
        # The reference window is read even though it is never reported,
        # otherwise the opening hours of any range have no p99 at all.
        return [symbol, start - self.config.wall_ref_ms, end, start]

    def _wall_fires(self, symbol: str, start: int, end: int) -> list[dict]:
        rows = self.con.execute(
            self._wall_sql(_WALL_FIRES_SQL), self._wall_params(start, end, symbol)
        ).fetchall()
        columns = [d[0] for d in self.con.description]
        return [dict(zip(columns, row)) for row in rows]

    def _wall_bars(self, symbol: str, start: int, end: int) -> dict[int, dict]:
        """Per-bar wall aggregate, keyed by bar_start."""
        rows = self.con.execute(
            self._wall_sql(_WALL_BARS_SQL), self._wall_params(start, end, symbol)
        ).fetchall()
        columns = [d[0] for d in self.con.description]
        return {int(r[0]): dict(zip(columns, r)) for r in rows}

    def walls(self, symbol: str, start: int, end: int) -> list[WallEvent]:
        """Merge consecutive samples of the same resting level into one event.

        The same wall reappears in every sample for as long as it rests, so
        raw fires over-count by roughly its lifetime in seconds. Detectors and
        the LLM summary both want "one order, this big, this close, for this
        long".
        """
        cfg = self.config
        gap = cfg.wall_sample_ms * 3  # tolerate a missed sample or two
        open_ev: dict[tuple[str, float], dict] = {}
        done: list[WallEvent] = []

        def close(key: tuple[str, float]) -> None:
            e = open_ev.pop(key)
            done.append(WallEvent(
                symbol=symbol, side=key[0], price=key[1],
                first_t=e["first_t"], last_t=e["last_t"], n_samples=e["n"],
                max_usd=e["max_usd"], max_ratio=e["max_ratio"],
                min_bps=e["min_bps"], ref_n=e["ref_n"],
            ))

        for f in self._wall_fires(symbol, start, end):
            key = (f["side"], f["price"])
            t = int(f["t"])
            for stale in [k for k, v in open_ev.items() if t - v["last_t"] > gap]:
                close(stale)
            e = open_ev.get(key)
            if e is None:
                open_ev[key] = {
                    "first_t": t, "last_t": t, "n": 1, "max_usd": f["usd"],
                    "max_ratio": f["ratio"], "min_bps": f["bps"],
                    "ref_n": int(f["ref_n"]),
                }
            else:
                e["last_t"] = t
                e["n"] += 1
                e["max_usd"] = max(e["max_usd"], f["usd"])
                e["max_ratio"] = max(e["max_ratio"], f["ratio"])
                e["min_bps"] = min(e["min_bps"], f["bps"])
                e["ref_n"] = max(e["ref_n"], int(f["ref_n"]))
        for key in list(open_ev):
            close(key)
        done.sort(key=lambda w: w.first_t)
        return done

    # -- level lifetime --------------------------------------------------

    def level_episodes(self, symbol: str, start: int, end: int) -> list[LevelEpisode]:
        """Track >= ``level_min_usd`` resting at a price until it disappears.

        Episodes are keyed by ``(side, price)``, never by book position. Size
        "at the best bid" is not comparable between snapshots unless the best
        bid price is unchanged - the top of book flickers between adjacent
        ticks carrying tiny sizes, and a ``lag(best_bid_qty)`` across that
        yields enormous multipliers that are pure artifact and look exactly
        like dramatic findings. Keying on the price is the general form of
        the ``best_bid = prev_bid`` constraint.

        Scoped to a bounded range on purpose - this is the one feature that
        needs every snapshot rather than a sampled grid, so it costs roughly
        one Python row per 100ms per symbol. Ask for hours, not days.
        """
        cfg = self.config
        n = self.con.execute(
            "SELECT count(*) FROM book WHERE symbol = ? AND t >= ? AND t < ?"
            + (" AND NOT est" if cfg.level_exclude_estimated else ""),
            [symbol, start, end],
        ).fetchone()[0]
        if n > cfg.level_max_snapshots:
            raise ValueError(
                f"{symbol}: {n:,} snapshots in range exceeds level_max_snapshots="
                f"{cfg.level_max_snapshots:,}; narrow the range or raise the limit"
            )

        sql = _SNAPSHOT_SQL.format(
            min_usd=cfg.level_min_usd,
            max_bps=cfg.level_max_bps,
            est_filter="AND NOT est" if cfg.level_exclude_estimated else "",
        )
        snapshots = self.con.execute(sql, [symbol, start, end]).fetchall()
        trades = self.con.execute(
            f"SELECT {cfg.trade_clock_column} AS t, price, qty, aggressive_buy "
            "FROM trades WHERE symbol = ? AND "
            f"{cfg.trade_clock_column} >= ? AND {cfg.trade_clock_column} < ? ORDER BY t",
            [symbol, start, end],
        ).fetchall()
        trade_times = [int(tr[0]) for tr in trades]

        open_levels: dict[tuple[str, float], dict] = {}
        done: list[LevelEpisode] = []
        prev_t = start

        def matched_qty(side: str, price: float, t0: int, t1: int) -> float:
            """Aggressive volume that could have consumed this level."""
            lo = bisect_left(trade_times, t0)
            hi = bisect_right(trade_times, t1)
            total = 0.0
            for _t, p, q, agg_buy in trades[lo:hi]:
                if side == "bid" and not agg_buy and p <= price + 1e-12:
                    total += q
                elif side == "ask" and agg_buy and p >= price - 1e-12:
                    total += q
            return total

        def close(key: tuple[str, float], t_close: int, t_open_window: int,
                  visible: bool) -> None:
            e = open_levels.pop(key)
            side, price = key
            if not visible:
                outcome, filled = Outcome.OUT_OF_BOOK, 0.0
            else:
                filled = matched_qty(side, price, t_open_window, t_close)
                resting_qty = e["last_usd"] / price if price else 0.0
                if filled >= cfg.level_fill_frac * resting_qty and filled > 0:
                    outcome = Outcome.FILLED
                elif filled > 0:
                    outcome = Outcome.PARTIAL
                else:
                    outcome = Outcome.CANCELLED
            done.append(LevelEpisode(
                symbol=symbol, side=side, price=price,
                first_t=e["first_t"], last_t=t_close,
                max_usd=e["max_usd"], last_usd=e["last_usd"],
                filled_qty=filled, outcome=outcome,
            ))

        for t, mid, _est, bid_floor, ask_ceil, big_bids, big_asks in snapshots:
            t = int(t)
            present: dict[tuple[str, float], float] = {}
            for lv in big_bids or []:
                present[("bid", lv["price"])] = lv["price"] * lv["qty"]
            for lv in big_asks or []:
                present[("ask", lv["price"])] = lv["price"] * lv["qty"]

            for key in list(open_levels):
                if key in present:
                    continue
                side, price = key
                # Did the level leave, or did the 20-level window move off it?
                visible = (price >= bid_floor) if side == "bid" else (price <= ask_ceil)
                close(key, t, prev_t, visible)

            for key, usd in present.items():
                e = open_levels.get(key)
                if e is None:
                    open_levels[key] = {"first_t": t, "max_usd": usd, "last_usd": usd}
                else:
                    e["max_usd"] = max(e["max_usd"], usd)
                    e["last_usd"] = usd
            prev_t = t

        for key in list(open_levels):
            e = open_levels.pop(key)
            done.append(LevelEpisode(
                symbol=symbol, side=key[0], price=key[1],
                first_t=e["first_t"], last_t=prev_t,
                max_usd=e["max_usd"], last_usd=e["last_usd"],
                filled_qty=0.0, outcome=Outcome.OPEN,
            ))
        done.sort(key=lambda e: e.first_t)
        return done


def _floor_bar(ms: int, bar_ms: int) -> int:
    return (ms // bar_ms) * bar_ms


# --------------------------------------------------------------------------
# diagnostic
# --------------------------------------------------------------------------

def _hhmm(ms: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).strftime("%m-%d %H:%M")


def _status_histogram(values: Iterable[Windowed]) -> str:
    counts: dict[str, int] = {}
    for w in values:
        counts[w.status.value] = counts.get(w.status.value, 0) + 1
    return "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def main(argv: list[str]) -> int:
    parquet_dir = Path(os.environ["MARKETGUARD_PARQUET"]) if os.environ.get(
        "MARKETGUARD_PARQUET") else config.PARQUET_DIR
    if not parquet_dir.exists():
        print(f"no parquet at {parquet_dir} - run: python etl.py")
        return 1

    cfg = FeatureConfig()
    fx = BatchFeatureExtractor(parquet_dir=parquet_dir, cfg=cfg)
    print(config.describe())
    print(f"parquet: {parquet_dir}\n")

    wanted = [a for a in argv if not a.startswith("-")]
    symbols = [wanted[0]] if wanted else fx.symbols()
    hours = int(wanted[1]) if len(wanted) > 1 else 24

    for symbol in symbols:
        cov = fx.coverage(symbol)
        if cov is None:
            print(f"{symbol}: no data")
            continue
        first, last = cov
        end = last + 1
        start = max(first + cfg.lookback_ms, end - hours * 3_600_000)

        bars = fx.bars(symbol, start, end, include_walls=True)
        if not bars:
            print(f"{symbol}: no bars in range")
            continue

        covered = [b for b in bars if b.covered]
        print(f"=== {symbol}  {_hhmm(start)} .. {_hhmm(end)}  "
              f"{len(bars)} bars ({len(bars) - len(covered)} uncovered) ===")
        print(f"  volume_z      {_status_histogram(b.volume_z for b in bars)}")
        print(f"  thin_book     {_status_histogram(b.thin_book for b in bars)}")
        print(f"  ofi           {_status_histogram(b.ofi for b in bars)}")
        print(f"  retracement   {_status_histogram(b.retracement for b in bars)}")
        print(f"  wall_ratio    {_status_histogram(b.wall_ratio for b in bars)}")

        med_depth = statistics.median([b.depth_usd for b in covered if b.depth_usd])
        print(f"  median depth +/-{cfg.depth_band_pct:.0%} of mid: ${med_depth:,.0f}")

        top = sorted((b for b in bars if b.volume_z.ok),
                     key=lambda b: b.volume_z.require(), reverse=True)[:3]
        for b in top:
            flags = ",".join(b.flags(cfg)) or "-"
            ofi_s = f"{b.ofi.value:+.3f}" if b.ofi.ok else b.ofi.status.value
            thin_s = f"{b.thin_book.value:.2f}" if b.thin_book.ok else b.thin_book.status.value
            print(f"  peak vol  {_hhmm(b.bar_start)}  z={b.volume_z.require():7.2f}"
                  f"  ${b.volume_usd:>12,.0f}  ofi={ofi_s}"
                  f"  thin_p={thin_s}"
                  f"  walls={b.wall_fires:<3}  [{flags}]")

        retr = sorted((b for b in bars if b.retracement.ok),
                      key=lambda b: b.retracement.require(), reverse=True)[:2]
        for b in retr:
            print(f"  retrace   {_hhmm(b.bar_start)}  ratio={b.retracement.require():.2f}"
                  f"  runup={b.runup_pct:+.2%}"
                  f"  peak={b.pump_peak:.4f} trough={b.pump_trough:.4f}")

        fired = [b for b in bars if b.flags(cfg)]
        print(f"  bars with any flag: {len(fired)} / {len(bars)}")

        # Walls and lifetimes are per-snapshot work, so scope them to the
        # busiest hour rather than the whole range.
        busiest = max(covered, key=lambda b: b.volume_usd)
        h0 = _floor_bar(busiest.bar_start, 3_600_000)
        events = fx.walls(symbol, h0, h0 + 3_600_000)
        if events:
            biggest = max(events, key=lambda w: w.max_usd)
            print(f"  walls {_hhmm(h0)}+1h: {len(events)} events, "
                  f"biggest ${biggest.max_usd:,.0f} at {biggest.min_bps:.1f}bps "
                  f"({biggest.max_ratio:.1f}x band p99) for {biggest.duration_ms:,}ms")
        else:
            print(f"  walls {_hhmm(h0)}+1h: none")

        try:
            stats = fx.level_stats(symbol, h0, h0 + 3_600_000)
        except ValueError as exc:
            print(f"  levels: {exc}")
        else:
            print(f"  levels {_hhmm(h0)}+1h: {stats.n_episodes:,} episodes  "
                  + "  ".join(f"{k}={v}" for k, v in stats.counts.items() if v))
            if stats.cancel_rate.ok:
                def ms(v):
                    return f"{v:,.0f}ms" if v is not None else "n/a"
                print(f"           cancel_rate={stats.cancel_rate.require():.3f}  "
                      f"median cancelled={ms(stats.median_cancelled_ms)}  "
                      f"p90={ms(stats.p90_cancelled_ms)}  "
                      f"median filled={ms(stats.median_filled_ms)}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
