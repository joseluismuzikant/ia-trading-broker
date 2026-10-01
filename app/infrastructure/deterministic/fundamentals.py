"""Deterministic fallback for the fundamental/news analysis stage.

The finalists stage wants an outside opinion on every candidate. There is no
news feed or language model configured yet, so this adapter produces one from
the readings the funnel already has: a company whose trend is up, whose
momentum is positive, and whose RSI is not stretched gets a higher score. It
needs no key and no network, so the whole flow runs and can be tested offline.

**This is not fundamental analysis and it is not news analysis.** It is a
documented placeholder that keeps the port honest until a real source plugs in
behind the same interface. Its summary says so, and its confidence is low on
purpose so the review page never presents it as more than it is.
"""

from __future__ import annotations

from app.domain.fundamentals import FundamentalView
from app.domain.strategy import RSI_BUY_ABOVE, RSI_OVERBOUGHT
from app.domain.trading import IndicatorEvidence
from app.domain.universe import Instrument

#: How sure the placeholder is of its own reading. A real source reports what
#: it actually knows; this one must never look confident.
PLACEHOLDER_CONFIDENCE = 0.2


class DeterministicFundamentals:
    """A fundamental view derived from price behaviour alone."""

    async def analyse(
        self, instrument: Instrument, evidence: IndicatorEvidence
    ) -> FundamentalView:
        """Score one instrument from its market readings, 0.0 to 1.0."""
        score = 0.5
        reasons: list[str] = []

        if evidence.ema_fast is not None and evidence.ema_slow is not None:
            if evidence.ema_fast > evidence.ema_slow:
                score += 0.2
                reasons.append("its trend is up")
            else:
                score -= 0.2
                reasons.append("its trend is down")

        if evidence.macd_histogram is not None:
            if evidence.macd_histogram > 0:
                score += 0.1
                reasons.append("momentum is positive")
            else:
                score -= 0.1
                reasons.append("momentum is negative")

        if evidence.rsi is not None:
            if RSI_BUY_ABOVE <= evidence.rsi < RSI_OVERBOUGHT:
                score += 0.1
                reasons.append("its RSI is in a healthy band")
            elif evidence.rsi >= RSI_OVERBOUGHT:
                score -= 0.1
                reasons.append("its RSI is stretched")

        score = max(0.0, min(1.0, score))
        summary = (
            f"{instrument.name} ({instrument.symbol}) is scored from price "
            "behaviour only, "
            + (", ".join(reasons) if reasons else "with no usable reading")
            + ". No news or company data was analysed."
        )
        return FundamentalView(
            score=score, summary=summary, confidence=PLACEHOLDER_CONFIDENCE
        )
