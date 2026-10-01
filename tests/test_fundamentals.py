"""Tests for the fundamental view, the port, and the deterministic fallback.

The fallback is a documented placeholder: it needs no key and no network so the
funnel runs end to end today. These tests pin what it promises (a 0.0-1.0 score
and an honest summary) and what a real adapter may replace.
"""

from __future__ import annotations

import pytest

from app.domain.fundamentals import FundamentalView
from app.domain.trading import IndicatorEvidence
from app.domain.universe import Instrument
from app.infrastructure.deterministic import DeterministicFundamentals
from app.ports.fundamentals import FundamentalAnalyzer


def instrument(symbol: str = "GGAL") -> Instrument:
    return Instrument(
        symbol=symbol,
        name="Grupo Financiero Galicia S.A.",
        category="argentina_stocks",
        market="BCBA",
    )


def evidence(
    *,
    fast: float | None = 110.0,
    slow: float | None = 100.0,
    rsi: float | None = 55.0,
    histogram: float | None = 0.5,
    ret: float | None = 0.08,
    observations: int = 60,
) -> IndicatorEvidence:
    return IndicatorEvidence(
        ema_fast=fast,
        ema_slow=slow,
        rsi=rsi,
        macd=1.0,
        macd_signal=0.5,
        macd_histogram=histogram,
        return_fraction=ret,
        observations=observations,
    )


async def test_the_fallback_is_an_analyser() -> None:
    assert isinstance(DeterministicFundamentals(), FundamentalAnalyzer)


async def test_the_fallback_scores_an_uptrend_higher_than_a_downtrend() -> None:
    analyser = DeterministicFundamentals()

    up = await analyser.analyse(instrument(), evidence())
    down = await analyser.analyse(
        instrument(), evidence(fast=90.0, slow=100.0, histogram=-0.5, ret=-0.2)
    )

    assert 0.0 <= down.score < up.score <= 1.0
    assert up.score > 0.5
    assert down.score < 0.5


async def test_the_fallback_is_neutral_when_it_has_nothing_to_read() -> None:
    analyser = DeterministicFundamentals()
    blank = IndicatorEvidence(observations=60)

    view = await analyser.analyse(instrument(), blank)

    assert view.score == pytest.approx(0.5)


async def test_a_stretched_rsi_costs_points() -> None:
    analyser = DeterministicFundamentals()

    healthy = await analyser.analyse(instrument(), evidence(rsi=55.0))
    stretched = await analyser.analyse(instrument(), evidence(rsi=85.0))

    assert healthy.score > stretched.score


async def test_the_summary_says_what_the_view_is_and_is_not() -> None:
    analyser = DeterministicFundamentals()

    view = await analyser.analyse(instrument(), evidence())

    assert "Grupo Financiero Galicia S.A." in view.summary
    assert "GGAL" in view.summary
    assert "No news or company data was analysed." in view.summary


async def test_the_fallback_stays_humble_about_its_own_confidence() -> None:
    analyser = DeterministicFundamentals()

    view = await analyser.analyse(instrument(), evidence())

    assert 0.0 <= view.confidence < 0.5


# --- The view itself ------------------------------------------------------


def test_the_view_bounds_are_enforced() -> None:
    with pytest.raises(ValueError, match="score"):
        FundamentalView(score=1.5, summary="too good")
    with pytest.raises(ValueError, match="confidence"):
        FundamentalView(score=0.5, summary="fine", confidence=-0.1)


def test_a_view_is_immutable() -> None:
    view = FundamentalView(score=0.5, summary="fine")

    with pytest.raises(Exception):
        view.score = 0.9  # type: ignore[misc]
