"""InvertirOnline (IOL) adapter.

Everything specific to the IOL REST API lives in this package. The rest of the
application only sees the typed models and the errors defined here.
"""

from app.infrastructure.iol.broker import IOLBroker, price_bar_from_quote
from app.infrastructure.iol.budget import CallUsage, call_budget
from app.infrastructure.iol.client import IOLClient, build_client
from app.infrastructure.iol.errors import (
    IOLAPIError,
    IOLAuthError,
    IOLBudgetExceededError,
    IOLConnectionError,
    IOLNotFoundError,
    IOLResponseError,
    IOLUnavailableError,
    safe_message,
)
from app.infrastructure.iol.mapping import portfolio_currency, to_portfolio
from app.infrastructure.iol.schemas import (
    AccountStatus,
    IOLInstrument,
    IOLPortfolio,
    PortfolioAsset,
    Profile,
    Quote,
    Token,
)

__all__ = [
    "AccountStatus",
    "CallUsage",
    "IOLAPIError",
    "IOLAuthError",
    "IOLBroker",
    "IOLBudgetExceededError",
    "IOLClient",
    "IOLConnectionError",
    "IOLInstrument",
    "IOLNotFoundError",
    "IOLPortfolio",
    "IOLResponseError",
    "IOLUnavailableError",
    "PortfolioAsset",
    "Profile",
    "Quote",
    "Token",
    "build_client",
    "price_bar_from_quote",
    "call_budget",
    "portfolio_currency",
    "safe_message",
    "to_portfolio",
]
