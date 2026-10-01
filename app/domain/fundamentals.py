"""The fundamental/news view of one instrument.

The view is what an outside analysis (a news feed, an LLM, or the
deterministic fallback in :mod:`app.infrastructure.deterministic.fundamentals`)
says about a company or a bond. It is deliberately a small, fixed shape: the
funnel only needs a score to rank with and a sentence to show in the review
page. A provider that cannot fill either one does not fit the port.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FundamentalView:
    """One outside opinion on one instrument.

    ``score`` runs from 0.0 (avoid) to 1.0 (attractive) and is what the
    finalists stage ranks on. ``summary`` is one short, human-readable sentence
    shown on the review page. ``confidence`` is how sure the source is of its
    own view, also 0.0-1.0, and is shown but never used to size an order.
    """

    score: float
    summary: str
    confidence: float = 0.0

    def __post_init__(self) -> None:
        for label, value in (
            ("score", self.score),
            ("confidence", self.confidence),
        ):
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{label} must be between 0.0 and 1.0")
