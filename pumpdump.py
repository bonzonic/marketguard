"""Pump & dump statistical gate.

The cheap filter in front of the LLM cascade. It consumes :mod:`features`
bars and emits *candidates* - windows where the conjunction the glossary
describes is simultaneously true:

    volume z-score high          unusual participation, on median/MAD so a
                                 previous pump in the trailing window cannot
                                 raise the bar against the next one
    price spike, vol-normalised  a 3% move is enormous on a placid pair and
                                 routine on a volatile one, so the move is
                                 scored against that symbol's own typical
                                 move rather than against a fixed percentage
    thin book                    depth within +/-1% of mid below the p20 of
                                 its own trailing distribution. Thin books
                                 are *targets*: they are cheap to move, which
                                 is why manipulators pick them
    one-sided order flow         OFI approaching +1.0 - almost every dollar
                                 traded in the window hit the ask
    no catalyst                  the absence of news is itself the signal

**This archive contains no pump and dump.** Measured across 51h of solusdt
the largest 30-minute run-up was 1.74% and the median 0.74%; every
``retracement > 0.7`` in the capture is noise retracing noise. So the
thresholds here cannot be validated against a positive example, and the
temptation to lower them until something fires must be refused: on this data
anything that fires is a false positive by construction. The numbers in
:class:`GateConfig` are the frozen design values, and
``python pumpdump.py --distributions`` prints what the features actually do
across the archive so that a later tuning pass - against data that contains a
real event - starts from measurements rather than from guesses.

**Probabilistic and confirmed are different types, not a flag.**
A live alert is a :class:`Candidate`: a conjunction of leading indicators,
which is a probability and never a finding. The retracement ratio
``(peak - trough) / (peak - baseline)`` is the confirming signal and it is
*retrospective* - it cannot be known at the moment the alert fires, because
the trough has not happened yet. Blurring the two is how a surveillance
system ends up accusing someone.

So :class:`Candidate` has no retracement field and no ``confirmed`` boolean;
:class:`ConfirmedDump` is a separate class that does not re-export the
candidate's fields (reach them through ``.candidate``), so code written
against one raises ``AttributeError`` on the other instead of quietly
rendering it. And the only constructor for a :class:`ConfirmedDump` is
:func:`confirm`, which *requires* bars extending past the candidate's own
window and raises :class:`PrematureConfirmation` otherwise. There is no code
path that turns a candidate into a confirmed event without data from the
future.

Usage:
    python pumpdump.py                     # gate over every symbol, 24h
    python pumpdump.py solusdt 72          # one symbol, 72 hours
    python pumpdump.py --distributions     # per-feature distributions
"""
from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

import config
import features as F
from features import (
    BarFeatures,
    FeatureConfig,
    InsufficientHistory,
    Status,
    Windowed,
)

# The degradation ladder (ok / partial / insufficient) is a project-wide
# policy, not a features.py implementation detail, and a second copy of it
# here would be a second place for "how short is too short" to drift. Reach
# for the one definition rather than restating it.
_history_status = F._history_status


# --------------------------------------------------------------------------
# catalyst seam
#
# Deliberately not implemented here. A news lookup is a network call with a
# rate limit and a bill attached, and it belongs behind the statistical gate,
# not inside it: checking news for every bar of every symbol is exactly the
# cost the cheap-filter-first architecture exists to avoid. The gate scores
# the four statistical legs, and whoever wires Tavily in supplies a
# CatalystSource.
# --------------------------------------------------------------------------

class Catalyst(str, Enum):
    """Whether a public explanation for the move was found.

    Three states, not a boolean, and the distinction between the first two is
    the whole point. ``ABSENT`` is a positive finding - somebody looked and
    there was no news - and it is the leg of the conjunction that makes a
    pump suspicious. ``UNKNOWN`` means nobody looked, or the lookup failed.
    Collapsing the two into ``not present`` would turn every outage into a
    manufactured signal.
    """

    UNKNOWN = "unknown"
    ABSENT = "absent"
    PRESENT = "present"


class CatalystSource(ABC):
    """Where a news check comes from. Implementations live elsewhere."""

    @abstractmethod
    def check(self, symbol: str, start_ms: int, end_ms: int) -> Catalyst:
        """Was there public news for ``symbol`` in ``[start_ms, end_ms)``?

        Implementations **must** return :attr:`Catalyst.UNKNOWN` on timeout,
        rate limit, or any other failure. Returning ``ABSENT`` when the
        lookup did not actually complete fabricates the no-catalyst signal,
        and it fabricates it precisely during the market-wide events that
        break news APIs.
        """


class UncheckedCatalyst(CatalystSource):
    """The default: nobody looked, and the output says so."""

    def check(self, symbol: str, start_ms: int, end_ms: int) -> Catalyst:
        return Catalyst.UNKNOWN


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GateConfig:
    """Thresholds for the conjunction.

    Every number here is a *design* value carried over from the glossary and
    chosen to mirror the cut points :mod:`features` already uses. None of
    them has been validated against a real pump, because the archive does not
    contain one. They are frozen on purpose: tuning them downwards until this
    capture produces a hit would be fitting noise, and the resulting detector
    would fire on ordinary market activity forever after.
    """

    # --- price spike ----------------------------------------------------
    # The move is measured over this many bars and scored against the same
    # symbol's own recent moves of the same length, so "3%" never appears as
    # a constant anywhere. Five minutes is short enough that a pump's
    # vertical leg fills it and long enough that a single print does not.
    spike_window_bars: int = 5
    spike_ref_bars: int = 60
    # Mirrors features.volume_notable_z. A 5-MAD move in five minutes is the
    # same order of unusual as a 5-MAD volume bar, and picking a different
    # number here would be asserting a relationship between the two that
    # nothing has measured.
    price_spike_z: float = 5.0

    # --- order flow -----------------------------------------------------
    # "Approaching +1.0". At +0.70, 85% of the window's notional hit the ask.
    # Sustained one-sided taking at that level is not two-sided price
    # discovery; it is one participant lifting the book.
    ofi_min: float = 0.70

    # --- volume ---------------------------------------------------------
    # Uses features.volume_notable_z / volume_extreme_z rather than
    # restating them, so there is exactly one definition of "notable".

    # --- confirmation (retrospective only) ------------------------------
    # How long after the candidate the dump is allowed to take. A pump that
    # holds for an hour and then fades is not the same pattern as one that
    # round-trips in ten minutes, and the difference is the whole distinction
    # between a manipulation and a rally.
    observation_bars: int = 60

    # --- degradation ----------------------------------------------------
    # A candidate built on PARTIAL_HISTORY is still emitted - suppressing it
    # would mean the detector goes quiet exactly when the recorder has just
    # restarted - but the status travels with it. Set this to
    # ``Status.OK`` to require full windows.
    max_status: Status = Status.PARTIAL_HISTORY

    def __post_init__(self) -> None:
        for name in ("spike_window_bars", "spike_ref_bars", "observation_bars"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        if not 0.0 < self.ofi_min <= 1.0:
            raise ValueError(f"ofi_min must be in (0, 1], got {self.ofi_min}")

    @property
    def lookback_bars(self) -> int:
        """Bars of history the gate needs before the first scored bar."""
        return self.spike_ref_bars + self.spike_window_bars


# --------------------------------------------------------------------------
# the vol-normalised price spike
#
# features.py does not compute this one: it is the gate's own notion of "big
# move for this symbol", and a detector-specific feature does not belong in
# the shared extractor where the spoofing detector would inherit it for no
# reason. The statistics are features.robust_z, unchanged.
# --------------------------------------------------------------------------

def window_returns(bars: Sequence[BarFeatures], k: int) -> list[float | None]:
    """``close[i] / close[i-k] - 1`` per bar, ``None`` where it cannot be had.

    ``None`` for the first ``k`` bars and for any bar whose close - or whose
    reference bar's close - is missing. An un-recorded minute has no price,
    and carrying the last known close across the hole would invent a move of
    exactly zero over a gap of unknown length.
    """
    closes = [b.close if b.covered else None for b in bars]
    out: list[float | None] = []
    for i, close in enumerate(closes):
        prev = closes[i - k] if i >= k else None
        if close is None or prev is None or prev <= 0:
            out.append(None)
        else:
            out.append(close / prev - 1.0)
    return out


def price_spike_zs(bars: Sequence[BarFeatures], cfg: GateConfig,
                   feature_cfg: FeatureConfig) -> list[Windowed]:
    """Signed, vol-normalised price spike per bar.

    The current ``k``-bar return, scored against the distribution of the
    *same symbol's* preceding ``k``-bar returns with median and MAD. That is
    what makes it vol-normalised: on a pair whose five-minute moves are
    typically 5bps a 3% leap is a colossal z, and on one that routinely moves
    1% it is unremarkable. No volatility model, no fixed percentage, and
    nothing that a quiet fortnight would silently recalibrate the wrong way.

    The reference windows overlap - consecutive ``k``-bar returns share
    ``k-1`` bars - so they are strongly autocorrelated. That is fine for what
    this is: a robust estimate of the scale of a typical move. It would not
    be fine if the z were being read as a p-value, and it is not.

    Sign is kept. A pump is a move *up*; a crash produces a large negative z
    and must not be allowed to satisfy a pump's spike leg.
    """
    k = cfg.spike_window_bars
    returns = window_returns(bars, k)
    out: list[Windowed] = []
    for i, value in enumerate(returns):
        lo = max(0, i - cfg.spike_ref_bars)
        reference = [r for r in returns[lo:i] if r is not None]
        if value is None:
            out.append(Windowed(None, Status.NO_DATA, len(reference),
                                cfg.spike_ref_bars))
            continue
        status = _history_status(len(reference), cfg.spike_ref_bars,
                                 feature_cfg.min_ref_frac)
        if status is Status.INSUFFICIENT_HISTORY:
            out.append(Windowed(None, status, len(reference), cfg.spike_ref_bars))
            continue
        z, zstatus = F.robust_z(value, reference)
        if zstatus is not Status.OK:
            status = zstatus
        out.append(Windowed(z, status, len(reference), cfg.spike_ref_bars))
    return out


# --------------------------------------------------------------------------
# outputs
#
# Two classes, on purpose. See the module docstring.
# --------------------------------------------------------------------------

class Severity(str, Enum):
    """How far past the notable cut the volume went. Not a confidence."""

    NOTABLE = "notable"
    EXTREME = "extreme"


@dataclass(frozen=True)
class Candidate:
    """A probabilistic, real-time pump alert. **Not** a finding.

    Everything here is knowable at ``bar_start + bar_ms`` and nothing here
    says a manipulation occurred. The honest reading is "the leading
    indicators of a pump are simultaneously true in this window"; the
    population that satisfies that includes genuine demand, index inclusions,
    and a whale with a market order.

    There is deliberately no ``retracement``, no ``confirmed`` and no
    ``verdict`` on this class, and ``frozen=True`` means no later code can
    add one: assignment raises ``FrozenInstanceError`` and a read raises
    ``AttributeError``. The retracement cannot exist yet - the trough
    has not happened - and a boolean would be one careless assignment away
    from presenting a guess as a fact. Confirmation is :func:`confirm`, which
    returns a different type.
    """

    symbol: str
    bar_start: int
    bar_ms: int

    severity: Severity
    volume_z: float
    price_spike_z: float
    thin_book_pctile: float
    ofi: float
    move_pct: float             # the raw k-bar return, for the explanation
    spike_window_bars: int

    catalyst: Catalyst
    status: Status              # the weakest window behind any of the above

    @property
    def window_end(self) -> int:
        return self.bar_start + self.bar_ms

    def headline(self) -> str:
        """One line that cannot be mistaken for a finding."""
        return (
            f"CANDIDATE (probabilistic, unconfirmed) {self.symbol} "
            f"{_hhmm(self.bar_start)}: volume z={self.volume_z:.1f}, "
            f"{self.move_pct:+.2%} over {self.spike_window_bars} bars "
            f"(z={self.price_spike_z:.1f}), depth at p"
            f"{self.thin_book_pctile * 100:.0f}, OFI {self.ofi:+.2f}, "
            f"catalyst {self.catalyst.value} [{self.status.value}]"
        )


class PrematureConfirmation(Exception):
    """Raised when confirmation was attempted without data from after the event.

    The retracement ratio needs the trough, and the trough is in the
    candidate's future. Asking for it with only the bars that produced the
    candidate is not a degraded answer, it is a category error, so this
    raises rather than returning ``None`` - a ``None`` would be
    indistinguishable from "looked, and it did not dump".
    """


@dataclass(frozen=True)
class ConfirmedDump:
    """A retrospective finding: the pump round-tripped.

    Note what this class does *not* do: re-export the candidate's fields.
    ``event.volume_z`` is an ``AttributeError``, and code written to render a
    candidate breaks loudly here rather than relabelling one as the other.
    Reach the leading indicators through :attr:`candidate`.

    Constructed only by :func:`confirm`.
    """

    candidate: Candidate
    retracement: float
    peak: float
    trough: float
    baseline: float
    runup_pct: float
    trough_at: int              # bar in which the low was observed
    observed_bars: int
    status: Status

    @property
    def symbol(self) -> str:
        return self.candidate.symbol

    def headline(self) -> str:
        return (
            f"CONFIRMED retracement {self.symbol} "
            f"{_hhmm(self.candidate.bar_start)}: ran up {self.runup_pct:+.2%} "
            f"to {self.peak:.6g}, fell to {self.trough:.6g} by "
            f"{_hhmm(self.trough_at)} - retraced {self.retracement:.0%} of the "
            f"run-up [{self.status.value}]"
        )


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------

_USABLE_ORDER = {Status.OK: 0, Status.ZERO_SCALE_FALLBACK: 1,
                 Status.PARTIAL_HISTORY: 2}


def _within(status: Status, limit: Status) -> bool:
    """Is ``status`` at least as trustworthy as ``limit``?"""
    if status not in _USABLE_ORDER:
        return False
    return _USABLE_ORDER[status] <= _USABLE_ORDER.get(limit, 2)


class PumpDumpGate:
    """The conjunction, over :class:`~features.FeatureExtractor` bars.

    Stateless with respect to the archive: give it bars and it scores them,
    so the same object works over the batch extractor today and over a
    streaming one later without changing a line. Nothing here reaches into
    Parquet or DuckDB directly.
    """

    def __init__(
        self,
        extractor: F.FeatureExtractor | None = None,
        cfg: GateConfig | None = None,
        catalyst: CatalystSource | None = None,
    ) -> None:
        self.extractor = extractor
        self.config = cfg or GateConfig()
        self.catalyst = catalyst or UncheckedCatalyst()

    # -- pure scoring ----------------------------------------------------

    def evaluate(self, bars: Sequence[BarFeatures]) -> list[Candidate]:
        """Score a contiguous, time-ordered run of bars for one symbol.

        The leading bars are consumed as history for the price-spike
        reference and are scored too - they simply come back
        ``INSUFFICIENT_HISTORY`` and produce nothing, which is the correct
        answer for a bar whose own history was never recorded.
        """
        if not bars:
            return []
        feature_cfg = self._feature_config()
        spikes = price_spike_zs(bars, self.config, feature_cfg)
        moves = window_returns(bars, self.config.spike_window_bars)
        out: list[Candidate] = []
        for bar, spike, move in zip(bars, spikes, moves):
            candidate = self._score(bar, spike, move, feature_cfg)
            if candidate is not None:
                out.append(candidate)
        return out

    def _feature_config(self) -> FeatureConfig:
        if self.extractor is not None:
            return self.extractor.config
        return FeatureConfig()

    def _score(self, bar: BarFeatures, spike: Windowed, move: float | None,
               feature_cfg: FeatureConfig) -> Candidate | None:
        cfg = self.config
        if not bar.covered or move is None:
            return None

        legs = (bar.volume_z, spike, bar.thin_book, bar.ofi)
        if not all(w.ok for w in legs):
            return None

        volume_z = bar.volume_z.require()
        spike_z = spike.require()
        thin = bar.thin_book.require()
        imbalance = bar.ofi.require()

        if volume_z <= feature_cfg.volume_notable_z:
            return None
        if spike_z <= cfg.price_spike_z:
            return None
        if thin >= feature_cfg.thin_book_pctile:
            return None
        if imbalance < cfg.ofi_min:
            return None

        status = F.worst(*(w.status for w in legs))
        if not _within(status, cfg.max_status):
            return None

        # The news check runs last, on the handful of bars that cleared four
        # statistical tests, which is the only reason it is affordable.
        verdict = self.catalyst.check(bar.symbol, bar.bar_start, bar.bar_end)
        if verdict is Catalyst.PRESENT:
            return None

        return Candidate(
            symbol=bar.symbol,
            bar_start=bar.bar_start,
            bar_ms=bar.bar_ms,
            severity=(Severity.EXTREME
                      if volume_z > feature_cfg.volume_extreme_z
                      else Severity.NOTABLE),
            volume_z=volume_z,
            price_spike_z=spike_z,
            thin_book_pctile=thin,
            ofi=imbalance,
            move_pct=move,
            spike_window_bars=cfg.spike_window_bars,
            catalyst=verdict,
            status=status,
        )

    # -- over an extractor -----------------------------------------------

    def scan(self, symbol: str, start: int, end: int) -> list[Candidate]:
        """Candidates in ``[start, end)``.

        Pulls extra history before ``start`` so the first scored bar has the
        same trailing reference as the last one; bars before ``start`` are
        used and discarded, never emitted.
        """
        if self.extractor is None:
            raise ValueError("scan() needs an extractor; use evaluate() otherwise")
        feature_cfg = self.extractor.config
        hist = self.config.lookback_bars * feature_cfg.bar_ms
        bars = self.extractor.bars(symbol, start - hist, end, include_walls=False)
        return [c for c in self.evaluate(bars) if start <= c.bar_start < end]


# --------------------------------------------------------------------------
# confirmation - retrospective, and structurally so
# --------------------------------------------------------------------------

def confirm(
    candidate: Candidate,
    bars: Sequence[BarFeatures],
    cfg: GateConfig | None = None,
    feature_cfg: FeatureConfig | None = None,
) -> ConfirmedDump | None:
    """Did the candidate's pump round-trip? Only answerable after the fact.

    ``bars`` must span from ``baseline_bars`` before the candidate to at
    least ``observation_bars`` after it. Anything shorter raises
    :class:`PrematureConfirmation` - including, in particular, the exact set
    of bars that produced the candidate. That is the structural guarantee:
    there is no way to obtain a :class:`ConfirmedDump` from a live window.

    Returns ``None`` when the window was fully observed and the pattern did
    not confirm - either there was no run-up worth the name or price held.
    ``None`` therefore means "looked, and no"; the exception means "cannot
    look yet". Raises :class:`~features.InsufficientHistory` when the
    baseline itself is unrecoverable, because a retracement measured against
    a fabricated baseline is a number with no meaning.
    """
    cfg = cfg or GateConfig()
    feature_cfg = feature_cfg or FeatureConfig()
    bar_ms = candidate.bar_ms

    ordered = sorted((b for b in bars if b.symbol == candidate.symbol),
                     key=lambda b: b.bar_start)
    if not ordered:
        raise PrematureConfirmation(
            f"no bars for {candidate.symbol}")

    baseline_start = candidate.bar_start - feature_cfg.baseline_bars * bar_ms
    required_end = candidate.bar_start + cfg.observation_bars * bar_ms

    if ordered[0].bar_start > baseline_start:
        raise PrematureConfirmation(
            f"baseline needs bars from {_hhmm(baseline_start)}; "
            f"earliest supplied is {_hhmm(ordered[0].bar_start)}")
    if ordered[-1].bar_start < required_end:
        raise PrematureConfirmation(
            f"confirmation needs bars through {_hhmm(required_end)}; "
            f"latest supplied is {_hhmm(ordered[-1].bar_start)} - the trough "
            f"has not happened yet")

    baseline_bars_ = [b for b in ordered
                      if baseline_start <= b.bar_start < candidate.bar_start]
    baseline_closes = [b.close for b in baseline_bars_
                       if b.covered and b.close is not None]
    status = _history_status(len(baseline_closes), feature_cfg.baseline_bars,
                             feature_cfg.min_ref_frac)
    if status is Status.INSUFFICIENT_HISTORY or not baseline_closes:
        raise InsufficientHistory(
            f"baseline: {len(baseline_closes)} of {feature_cfg.baseline_bars} bars")
    baseline = F.median(baseline_closes)

    window = [b for b in ordered
              if candidate.bar_start <= b.bar_start <= required_end]
    highs = [b.high if b.covered else None for b in window]
    lows = [b.low if b.covered else None for b in window]
    observed = sum(1 for h in highs if h is not None)
    status = F.worst(status,
                     _history_status(observed, cfg.observation_bars,
                                     feature_cfg.min_ref_frac))
    if status is Status.INSUFFICIENT_HISTORY:
        raise InsufficientHistory(
            f"observation window: {observed} of {cfg.observation_bars} bars")

    ratio, peak, trough = F.retracement(highs, lows, baseline)
    if ratio is None or peak is None or trough is None or baseline <= 0:
        return None

    runup = peak / baseline - 1.0
    if runup < feature_cfg.min_runup_pct:
        return None
    if ratio <= feature_cfg.retracement_fires_at:
        return None

    trough_at = next(b.bar_start for b in window
                     if b.covered and b.low is not None and b.low == trough)

    return ConfirmedDump(
        candidate=candidate,
        retracement=ratio,
        peak=peak,
        trough=trough,
        baseline=baseline,
        runup_pct=runup,
        trough_at=trough_at,
        observed_bars=observed,
        status=status,
    )


# --------------------------------------------------------------------------
# distributions
#
# The only honest form of evidence available on an archive with no positive
# example: not "does it fire" but "what values does this data actually
# produce, and are the frozen thresholds anywhere near them".
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Distribution:
    """Quantiles of one feature over the bars where it was computable."""

    name: str
    n: int
    n_unusable: int
    q: dict[str, float]
    n_over: int          # bars at or past the gate's threshold
    threshold: float | None

    def line(self) -> str:
        if not self.n:
            return f"  {self.name:<14} no usable bars ({self.n_unusable} unusable)"
        q = self.q
        over = ""
        if self.threshold is not None:
            over = f"  past {self.threshold:g}: {self.n_over} ({self.n_over / self.n:.3%})"
        return (f"  {self.name:<14} n={self.n:<6} (unusable {self.n_unusable:<5}) "
                f"min={q['min']:>8.3f} p50={q['p50']:>8.3f} p90={q['p90']:>8.3f} "
                f"p99={q['p99']:>8.3f} p999={q['p999']:>8.3f} max={q['max']:>8.3f}"
                f"{over}")


def distribution(name: str, values: Sequence[Windowed],
                 threshold: float | None = None,
                 above: bool = True) -> Distribution:
    """Quantiles over the usable values, counting the unusable ones.

    The unusable count is reported rather than dropped: "p99 of volume_z is
    3.1" means something very different over 4,000 bars than over 40, and the
    difference is invisible unless the gaps are on the page.
    """
    usable = [w.require() for w in values if w.ok]
    return _distribution(name, usable, len(values) - len(usable), threshold, above)


def _distribution(name: str, usable: list[float], unusable: int,
                  threshold: float | None, above: bool) -> Distribution:
    if not usable:
        return Distribution(name, 0, unusable, {}, 0, threshold)
    q = {
        "min": min(usable),
        "p50": F.quantile(usable, 0.50),
        "p90": F.quantile(usable, 0.90),
        "p99": F.quantile(usable, 0.99),
        "p999": F.quantile(usable, 0.999),
        "max": max(usable),
    }
    if threshold is None:
        n_over = 0
    elif above:
        n_over = sum(1 for v in usable if v > threshold)
    else:
        n_over = sum(1 for v in usable if v < threshold)
    return Distribution(name, len(usable), unusable, q, n_over, threshold)


def describe(bars: Sequence[BarFeatures], cfg: GateConfig,
             feature_cfg: FeatureConfig) -> list[Distribution]:
    """What each leg of the conjunction actually does over these bars."""
    spikes = price_spike_zs(bars, cfg, feature_cfg)
    return [
        distribution("volume_z", [b.volume_z for b in bars],
                     feature_cfg.volume_notable_z, True),
        distribution("price_spike_z", spikes, cfg.price_spike_z, True),
        distribution("thin_book_p", [b.thin_book for b in bars],
                     feature_cfg.thin_book_pctile, False),
        distribution("ofi", [b.ofi for b in bars], cfg.ofi_min, True),
        distribution("retracement", [b.retracement for b in bars],
                     feature_cfg.retracement_fires_at, True),
    ]


# --------------------------------------------------------------------------
# diagnostic
# --------------------------------------------------------------------------

def _hhmm(ms: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).strftime("%m-%d %H:%M")


def _conjunction_counts(bars: Sequence[BarFeatures], cfg: GateConfig,
                        feature_cfg: FeatureConfig) -> str:
    """How far down the conjunction this data ever gets.

    Reported cumulatively, because "volume fired 40 times and OFI fired 900
    times" says nothing about whether they ever fired *together*, and the
    conjunction is the entire design.
    """
    spikes = price_spike_zs(bars, cfg, feature_cfg)
    steps = [
        ("covered", lambda b, s: b.covered),
        ("+ volume_z", lambda b, s: b.volume_z.ok
            and b.volume_z.require() > feature_cfg.volume_notable_z),
        ("+ spike", lambda b, s: s.ok and s.require() > cfg.price_spike_z),
        ("+ thin", lambda b, s: b.thin_book.ok
            and b.thin_book.require() < feature_cfg.thin_book_pctile),
        ("+ ofi", lambda b, s: b.ofi.ok and b.ofi.require() >= cfg.ofi_min),
    ]
    alive = list(zip(bars, spikes))
    parts = []
    for label, test in steps:
        alive = [(b, s) for b, s in alive if test(b, s)]
        parts.append(f"{label}={len(alive)}")
    return "  ".join(parts)


def main(argv: list[str]) -> int:
    parquet_dir = Path(os.environ["MARKETGUARD_PARQUET"]) if os.environ.get(
        "MARKETGUARD_PARQUET") else config.PARQUET_DIR
    if not parquet_dir.exists():
        print(f"no parquet at {parquet_dir} - run: python etl.py")
        return 1

    want_dist = "--distributions" in argv
    wanted = [a for a in argv if not a.startswith("-")]

    feature_cfg = FeatureConfig()
    cfg = GateConfig()
    fx = F.BatchFeatureExtractor(parquet_dir=parquet_dir, cfg=feature_cfg)
    gate = PumpDumpGate(fx, cfg)

    print(config.describe())
    print(f"parquet: {parquet_dir}")
    print(f"thresholds: volume_z>{feature_cfg.volume_notable_z:g} "
          f"spike_z>{cfg.price_spike_z:g} "
          f"thin<p{feature_cfg.thin_book_pctile * 100:.0f} "
          f"ofi>={cfg.ofi_min:g}  (frozen; this archive contains no pump)\n")

    symbols = [wanted[0]] if wanted else fx.symbols()
    hours = int(wanted[1]) if len(wanted) > 1 else 24

    total_candidates = 0
    for symbol in symbols:
        cov = fx.coverage(symbol)
        if cov is None:
            print(f"{symbol}: no data")
            continue
        first, last = cov
        end = last + 1
        hist = cfg.lookback_bars * feature_cfg.bar_ms
        start = max(first + feature_cfg.lookback_ms + hist,
                    end - hours * 3_600_000)

        bars = fx.bars(symbol, start - hist, end, include_walls=False)
        if not bars:
            print(f"{symbol}: no bars in range")
            continue
        scored = [b for b in bars if b.bar_start >= start]

        print(f"=== {symbol}  {_hhmm(start)} .. {_hhmm(end)}  "
              f"{len(scored)} bars "
              f"({sum(1 for b in scored if not b.covered)} uncovered) ===")

        if want_dist:
            for d in describe(scored, cfg, feature_cfg):
                print(d.line())

        print(f"  conjunction   {_conjunction_counts(scored, cfg, feature_cfg)}")

        candidates = [c for c in gate.evaluate(bars) if c.bar_start >= start]
        total_candidates += len(candidates)
        if not candidates:
            print("  candidates: none")
        for c in candidates:
            print(f"  {c.headline()}")
            try:
                event = confirm(c, bars, cfg, feature_cfg)
            except (PrematureConfirmation, InsufficientHistory) as exc:
                print(f"    not yet confirmable: {exc}")
            else:
                print(f"    {event.headline()}" if event
                      else "    looked, and it did not retrace")
        print()

    print(f"total candidates across {len(symbols)} symbol(s): {total_candidates}")
    if total_candidates:
        print("A candidate on this archive is a false positive by construction - "
              "investigate it rather than accepting it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
