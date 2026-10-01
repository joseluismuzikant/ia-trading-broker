"""The fundamental/news analysis port.

An :class:`FundamentalAnalyzer` turns one instrument and its market readings
into a :class:`~app.domain.fundamentals.FundamentalView`. It is the only seam
through which the funnel reaches an outside opinion, so the deterministic
fallback used now and a news-plus-LLM adapter on a later day share it.

The port says nothing about a language model, a news feed, or an API key. It is
called once per candidate, never for the whole universe, which is what keeps an
expensive source affordable.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain.fundamentals import FundamentalView
from app.domain.trading import IndicatorEvidence
from app.domain.universe import Instrument


@runtime_checkable
class FundamentalAnalyzer(Protocol):
    """Produce the outside view the finalists stage ranks on.

    Implementations must answer for one instrument at a time and may raise
    when a source is unavailable: the funnel then falls back to the technical
    score alone rather than deleting the instrument.
    """

    async def analyse(
        self, instrument: Instrument, evidence: IndicatorEvidence
    ) -> FundamentalView:
        """Return one view for one instrument."""
        ...
