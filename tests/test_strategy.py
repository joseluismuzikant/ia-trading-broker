"""Tests for the rule-based strategy."""

from __future__ import annotations

import pytest

from app.domain.money import money
from app.domain.strategy import decide
from app.domain.trading import IndicatorEvidence


def evidence(**overrides) -> IndicatorEvidence:
    """A full, readable set of indicators that the tests adjust per case."""
    values = {
        "last_price": money("100.00"),
        "ema_fast": 110.0,
        "ema_slow": 100.0,
        "rsi": 55.0,
        "macd": 2.0,
        "macd_signal": 1.0,
        "macd_histogram": 1.0,
        "observations": 60,
    }
    values.update(overrides)
    return IndicatorEvidence(**values)


def test_an_uptrend_with_positive_momentum_buys() -> None:
    decision = decide(evidence())
    assert decision.action == "BUY"
    assert "Uptrend" in decision.rationale


def test_a_downtrend_with_weak_rsi_sells() -> None:
    decision = decide(evidence(ema_fast=90.0, ema_slow=100.0, rsi=40.0, macd_histogram=-1.0))
    assert decision.action == "SELL"
    assert "Downtrend" in decision.rationale


def test_an_overbought_uptrend_holds() -> None:
    decision = decide(evidence(rsi=75.0))
    assert decision.action == "HOLD"
    assert "overbought" in decision.rationale


def test_an_uptrend_without_positive_momentum_holds() -> None:
    decision = decide(evidence(macd_histogram=-0.5))
    assert decision.action == "HOLD"


def test_a_downtrend_with_strong_rsi_holds() -> None:
    # RSI still above the sell line: momentum has not faded yet.
    decision = decide(evidence(ema_fast=90.0, ema_slow=100.0, rsi=60.0))
    assert decision.action == "HOLD"


@pytest.mark.parametrize(
    "missing",
    [
        {"ema_fast": None},
        {"ema_slow": None},
        {"rsi": None},
        {"macd_histogram": None},
    ],
)
def test_a_missing_indicator_never_trades(missing) -> None:
    decision = decide(evidence(**missing))
    assert decision.action == "HOLD"
    assert "Not enough history" in decision.rationale


def test_a_flat_market_holds() -> None:
    decision = decide(evidence(ema_fast=100.0, ema_slow=100.0))
    assert decision.action == "HOLD"
