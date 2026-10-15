"""Simulated execution and advice adapters.

The paper adapter fills orders locally against the saved paper ledger, and the
fake portfolio advisor answers with a valid allocation instead of asking a
model. Neither needs credentials and neither makes a network call, so the whole
flow can run and be tested offline.
"""

from app.infrastructure.simulation.paper import PaperOrderExecutor
from app.infrastructure.simulation.paper_broker import PaperBroker
from app.infrastructure.simulation.portfolio_advisor import (
    FakePortfolioAdvisor,
    advice_from_targets,
    keep_portfolio,
)

__all__ = [
    "FakePortfolioAdvisor",
    "PaperBroker",
    "PaperOrderExecutor",
    "advice_from_targets",
    "keep_portfolio",
]
