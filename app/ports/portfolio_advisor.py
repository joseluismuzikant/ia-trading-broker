"""The portfolio-advice port.

A :class:`PortfolioAdvisor` turns one :class:`PortfolioAnalysisContext` — the
account as a whole — into a :class:`PortfolioAdvice`: a complete target
allocation of the money already there. It is the only seam through which the
application reaches an outside opinion, so the OpenAI adapter and the
deterministic fallback share it.

The port says nothing about a language model, a news feed, or an API key, and
it never returns quantities or broker requests. It is called **once per
analysis for the whole portfolio**, never once per instrument, which is what
keeps an expensive source affordable and lets the model reason about the
portfolio as one whole.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain.analysis import PortfolioAdvice, PortfolioAnalysisContext


@runtime_checkable
class PortfolioAdvisor(Protocol):
    """Produce the target allocation the review page will show.

    Implementations answer for the whole portfolio in one call and may raise
    when a source is unavailable: the caller then falls back to another
    implementation rather than inventing an allocation.
    """

    async def analyze(self, context: PortfolioAnalysisContext) -> PortfolioAdvice:
        """Return one target allocation for one portfolio."""
        ...
