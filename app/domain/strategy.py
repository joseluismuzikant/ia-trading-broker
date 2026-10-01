"""The rule-based strategy.

This is fixed Python. It reads indicator values and returns a buy, sell, or hold
with a one-line reason. It does not size the order and it does not check risk:
those are separate steps, so a change to one cannot silently change the other.

The rules are deliberately simple and fully determined by their inputs:

* **Buy** when the fast EMA is above the slow EMA, RSI is between 45 and 70, and
  the MACD histogram has turned positive. That is an uptrend that is not already
  overbought.
* **Sell** when the fast EMA has fallen below the slow EMA and RSI is below 50.
  That is a trend that has rolled over.
* **Hold** in every other case, including when there is not enough history to
  compute the indicators.

A missing indicator never produces a trade. Guessing is worse than doing nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.domain.trading import Action, IndicatorEvidence

#: RSI at or above this is overbought, so no new buy is opened.
RSI_OVERBOUGHT = 70.0
#: RSI at or below this confirms momentum has faded, so a sell is allowed.
RSI_SELL_BELOW = 50.0
#: A buy also wants RSI at or above this, so a falling market is not bought only
#: because two averages have not crossed yet.
RSI_BUY_ABOVE = 45.0

DecisionAction = Literal["BUY", "SELL", "HOLD"]


@dataclass(frozen=True)
class StrategyDecision:
    """The strategy's choice for one instrument, before sizing."""

    action: Action
    rationale: str


def decide(evidence: IndicatorEvidence) -> StrategyDecision:
    """Choose buy, sell, or hold from one instrument's indicator readings."""
    missing = _missing(evidence)
    if missing:
        return StrategyDecision(
            "HOLD",
            "Not enough history to judge a trend "
            f"({', '.join(missing)} unavailable), so no trade is proposed.",
        )

    assert evidence.ema_fast is not None
    assert evidence.ema_slow is not None
    assert evidence.rsi is not None
    assert evidence.macd_histogram is not None

    uptrend = evidence.ema_fast > evidence.ema_slow
    downtrend = evidence.ema_fast < evidence.ema_slow

    if (
        uptrend
        and evidence.macd_histogram > 0
        and RSI_BUY_ABOVE <= evidence.rsi < RSI_OVERBOUGHT
    ):
        return StrategyDecision(
            "BUY",
            "Uptrend: the fast average is above the slow average, momentum has "
            "turned positive, and RSI is not overbought.",
        )

    if downtrend and evidence.rsi < RSI_SELL_BELOW:
        return StrategyDecision(
            "SELL",
            "Downtrend: the fast average has crossed below the slow average and "
            "RSI shows momentum has faded.",
        )

    return StrategyDecision("HOLD", _hold_reason(evidence, uptrend=uptrend))


def _missing(evidence: IndicatorEvidence) -> list[str]:
    """Names of the readings the rules need and do not have."""
    missing: list[str] = []
    if evidence.ema_fast is None or evidence.ema_slow is None:
        missing.append("EMA")
    if evidence.rsi is None:
        missing.append("RSI")
    if evidence.macd_histogram is None:
        missing.append("MACD")
    return missing


def _hold_reason(evidence: IndicatorEvidence, *, uptrend: bool) -> str:
    """Explain which rule kept the strategy out of a trade."""
    assert evidence.rsi is not None
    if uptrend and evidence.rsi >= RSI_OVERBOUGHT:
        return (
            f"Uptrend, but RSI is {evidence.rsi:.1f}, which is overbought, so no "
            "new buy is opened."
        )
    if uptrend:
        return "Uptrend, but momentum has not turned positive, so no buy yet."
    return "No clear trend in either direction, so the position is left unchanged."
