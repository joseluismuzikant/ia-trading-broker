"""The broker read port.

A :class:`BrokerReader` is everything the application needs to *read* from a
broker: the portfolio, the cash, price history, and the state of an operation.
It speaks only the application's own types, so a broker is one adapter that
implements it (IOL today, PPI and a paper account next) and no service has to
know a vendor's field names, endpoints or token rules.

Writes are not here. An order goes through :class:`~app.ports.execution.OrderExecutor`,
which only ``app/services/execution.py`` may call.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from app.domain.portfolio import Portfolio


class BrokerError(Exception):
    """Base class for every failure of a broker adapter.

    The message is short and safe to show: it never carries a password, a
    token, or a response body. Adapters subclass this so a service can catch one
    type whichever broker answered.
    """

    default_message = "The broker request failed."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class BrokerUnavailableError(BrokerError):
    """The adapter cannot run: an unsupported broker, or a missing setting."""

    default_message = "This broker is not available."


@dataclass(frozen=True)
class CashBalance:
    """Free cash in one currency, as the broker reports it."""

    currency: str | None
    available: Decimal


@dataclass(frozen=True)
class PriceBar:
    """One day of price history, in the application's own shape."""

    #: The bar's timestamp as the broker gave it (ISO text), used only for ordering.
    at: str | None
    close: float | None
    high: float | None = None
    low: float | None = None
    volume: float | None = None


@runtime_checkable
class BrokerReader(Protocol):
    """Read one broker account. Safe to call repeatedly: nothing here sends an order."""

    async def authenticate(self) -> None:
        """Prove the credentials work, without reading any account data."""
        ...

    async def get_portfolio(self, country: str) -> Portfolio:
        """The holdings for one country, in the shared portfolio format.

        Cash is left unset when the broker does not report it with the holdings;
        :meth:`get_balances` is where cash comes from.
        """
        ...

    async def get_balances(self) -> list[CashBalance]:
        """Free cash, one entry per currency."""
        ...

    async def get_history(self, *, symbol: str, market: str, days: int) -> list[PriceBar]:
        """Daily price history for one instrument, oldest or newest first."""
        ...

    async def get_operation(self, broker_order_id: str) -> dict[str, Any]:
        """What the broker says happened to one operation (a read, never a write)."""
        ...

    async def aclose(self) -> None:
        """Release the connection."""
        ...
