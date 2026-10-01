"""Scanning, ranking, and narrowing the configured universe into trade ideas.

The analysis funnel is staged so the expensive stages stay small. The universe
holds 34 instruments, and each stage may keep only so many of them:

1. **Market scanner** (34 in, 10-12 out) — cheap price-history readings:
   how much trades, how far it moved, and whether the trend is up.
2. **Technical analysis** (same 10-12) — the indicators the strategy reads,
   scored on one scale.
3. **Finalists** (5 out) — the technical score combined with the
   fundamental/news view, so an outside opinion can promote or demote a name
   before anything is sized.

What is left is handed to the risk manager, which turns at most a few of them
into orders. Nothing here reads a snapshot, calls a broker, or knows what an
approval is: inputs are plain values and domain models, output is candidates
and finalists.

Scores run from 0.0 to 1.0 everywhere, so the two views in the finalists stage
can be combined without pretending one is ten times more important than the
other because of its scale.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import Mapping, Sequence

from app.domain.fundamentals import FundamentalView
from app.domain.strategy import RSI_BUY_ABOVE, RSI_OVERBOUGHT
from app.domain.trading import IndicatorEvidence
from app.domain.universe import Instrument

#: A ranking needs this many closes before its indicators are trusted at all.
MIN_OBSERVATIONS = 40

#: Share of the finalists' score that comes from the outside view. The
#: technical score keeps the larger say, because it is the only one of the two
#: that is measured from the market the plan actually trades in.
FUNDAMENTAL_WEIGHT = 0.4

#: Score assumed for an instrument whose outside analysis is missing.
NEUTRAL_FUNDAMENTAL_SCORE = 0.5

#: The scanner's own weights: trend first, then how far it already moved, then
#: how much of it actually trades.
SCAN_TREND_WEIGHT = 0.4
SCAN_MOMENTUM_WEIGHT = 0.4
SCAN_LIQUIDITY_WEIGHT = 0.2


@dataclass(frozen=True)
class Candidate:
    """One instrument the market scanner kept, with the readings behind it."""

    instrument: Instrument
    evidence: IndicatorEvidence
    scan_score: float

    @property
    def symbol(self) -> str:
        return self.instrument.symbol

    @property
    def name(self) -> str:
        return self.instrument.name

    @property
    def category(self) -> str:
        return self.instrument.category


@dataclass(frozen=True)
class Finalist:
    """One instrument that survived every funnel stage, with both views on it."""

    candidate: Candidate
    technical_score: float
    fundamental_score: float
    fundamental_summary: str
    combined_score: float

    @property
    def symbol(self) -> str:
        return self.candidate.symbol

    @property
    def name(self) -> str:
        return self.candidate.name

    @property
    def category(self) -> str:
        return self.candidate.category

    @property
    def evidence(self) -> IndicatorEvidence:
        return self.candidate.evidence


def scan_market(
    instruments: Sequence[Instrument],
    evidence_by_symbol: Mapping[str, IndicatorEvidence],
    *,
    limit: int,
) -> list[Candidate]:
    """Funnel stage one: the whole universe reduced to its best ``limit`` names.

    An instrument is only considered when its history is long enough to judge
    and its price is real. Of those, the ones with an uptrend, a move already
    behind them, and real traded volume lead. Volume is compared within this
    universe only, so a bond that trades less than a share is not discarded
    just for being a bond.
    """
    usable: list[tuple[Instrument, IndicatorEvidence]] = []
    for instrument in instruments:
        evidence = evidence_by_symbol.get(instrument.symbol)
        if evidence is None or not is_judgable(evidence):
            continue
        usable.append((instrument, evidence))

    volumes = sorted(_volume_of(evidence) for _, evidence in usable)

    scored: list[Candidate] = []
    for instrument, evidence in usable:
        volume = _volume_of(evidence)
        liquidity = bisect_left(volumes, volume) / max(len(volumes) - 1, 1)
        trend = 1.0 if _is_uptrend(evidence) else 0.0
        momentum = _clamp(evidence.return_fraction or 0.0)
        score = (
            SCAN_TREND_WEIGHT * trend
            + SCAN_MOMENTUM_WEIGHT * momentum
            + SCAN_LIQUIDITY_WEIGHT * liquidity
        )
        scored.append(
            Candidate(instrument=instrument, evidence=evidence, scan_score=score)
        )

    scored.sort(
        key=lambda row: (
            -row.scan_score,
            -_volume_of(row.evidence),
            row.symbol,
        )
    )
    return scored[: max(limit, 0)]


def technical_score(evidence: IndicatorEvidence | None) -> float | None:
    """Funnel stage two: how attractive one instrument is on the indicators.

    An uptrend is worth the most, positive momentum next, a healthy RSI band
    after that. An instrument the strategy would refuse to buy scores low here
    too, which keeps the technical stage and the strategy from arguing with each
    other. None means the reading is not good enough to judge at all.
    """
    if evidence is None or not is_judgable(evidence):
        return None
    if (
        evidence.ema_fast is None
        or evidence.ema_slow is None
        or evidence.rsi is None
        or evidence.macd_histogram is None
    ):
        return None

    score = 0.0
    if evidence.ema_fast > evidence.ema_slow:
        score += 0.5
    if evidence.macd_histogram > 0:
        score += 0.3
    if RSI_BUY_ABOVE <= evidence.rsi < RSI_OVERBOUGHT:
        score += 0.2
    elif evidence.rsi < RSI_OVERBOUGHT:
        score += 0.1
    return _clamp(score + 0.1 * _clamp(evidence.return_fraction or 0.0))


def select_finalists(
    candidates: Sequence[Candidate],
    views: Mapping[str, FundamentalView],
    *,
    limit: int,
) -> list[Finalist]:
    """Funnel stage three: technical plus outside view, cut to ``limit``.

    A candidate without a view is ranked on its technical score alone, at a
    neutral outside score, so one missing analysis never deletes a name the
    market scanner already chose.
    """
    finalists: list[Finalist] = []
    for candidate in candidates:
        technical = technical_score(candidate.evidence)
        if technical is None:
            continue
        view = views.get(candidate.symbol)
        fundamental = view.score if view is not None else NEUTRAL_FUNDAMENTAL_SCORE
        combined = (1.0 - FUNDAMENTAL_WEIGHT) * technical + FUNDAMENTAL_WEIGHT * fundamental
        finalists.append(
            Finalist(
                candidate=candidate,
                technical_score=technical,
                fundamental_score=fundamental,
                fundamental_summary=(
                    view.summary if view is not None else "No outside view was produced."
                ),
                combined_score=combined,
            )
        )

    finalists.sort(
        key=lambda row: (-row.combined_score, -row.technical_score, row.symbol)
    )
    return finalists[: max(limit, 0)]


def is_judgable(evidence: IndicatorEvidence) -> bool:
    """True when a reading is long enough and priced enough to be scored."""
    return (
        evidence.observations >= MIN_OBSERVATIONS
        and evidence.last_price is not None
        and evidence.last_price.is_positive
    )


def _is_uptrend(evidence: IndicatorEvidence) -> bool:
    return (
        evidence.ema_fast is not None
        and evidence.ema_slow is not None
        and evidence.ema_fast > evidence.ema_slow
    )


def _volume_of(evidence: IndicatorEvidence) -> float:
    return float(evidence.average_volume or 0.0)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
