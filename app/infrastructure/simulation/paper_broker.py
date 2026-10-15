"""A simulated account as a :class:`~app.ports.broker_reader.BrokerReader`.

A paper account holds cash and positions of its own (saved by the ledger
service) and takes market prices from one of the user's real connections. It
reads like any broker, so the analysis, the review and the approval work on it
unchanged, and it sends nothing anywhere: its orders are filled locally by
:class:`~app.infrastructure.simulation.paper.PaperOrderExecutor`.

The account is loaded through a callable and the prices through another reader,
so this class needs no database and no vendor code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.domain.money import normalise_currency
from app.domain.portfolio import Instrument, Portfolio, Position
from app.domain.trading import PaperLedger
from app.ports.broker_reader import (
    BrokerError,
    BrokerReader,
    BrokerUnavailableError,
    CashBalance,
    PriceBar,
)


class PaperBroker:
    """Read one paper account."""

    def __init__(
        self,
        *,
        country: str,
        load_account: Callable[[str], Awaitable[PaperLedger | None]],
        price_source: BrokerReader | None,
    ) -> None:
        self._country = country
        self._load_account = load_account
        self._price_source = price_source

    async def _account(self, country: str) -> PaperLedger:
        ledger = await self._load_account(country)
        if ledger is None:
            raise BrokerError(
                "This paper account has not been started for this country. "
                "Reset it from its connection page."
            )
        return ledger

    async def authenticate(self) -> None:
        # Nothing to log in to. The prices are checked when they are read.
        await self._account(self._country)

    async def get_portfolio(self, country: str) -> Portfolio:
        ledger = await self._account(country)
        return Portfolio(
            country=ledger.country,
            currency=ledger.currency,
            cash=float(ledger.cash.amount),
            positions=[
                Position(
                    instrument=Instrument(
                        symbol=position.symbol,
                        market=position.market,
                        currency=position.currency or ledger.currency,
                    ),
                    total_quantity=float(position.quantity),
                    available_quantity=float(position.quantity),
                    committed_quantity=0.0,
                    last_price=float(position.last_price.amount),
                    average_price=float(position.average_price.amount),
                    market_value=float(position.market_value.amount),
                )
                for position in ledger.positions
            ],
        )

    async def get_balances(self) -> list[CashBalance]:
        ledger = await self._account(self._country)
        return [
            CashBalance(currency=normalise_currency(ledger.currency), available=ledger.cash.amount)
        ]

    async def get_history(self, *, symbol: str, market: str, days: int) -> list[PriceBar]:
        if self._price_source is None:
            raise BrokerUnavailableError(
                "This paper account has no connection to take prices from."
            )
        return await self._price_source.get_history(symbol=symbol, market=market, days=days)

    async def get_operation(self, broker_order_id: str) -> dict[str, Any]:
        raise BrokerError("A paper order is filled at once and has no broker operation.")

    async def aclose(self) -> None:
        if self._price_source is not None:
            await self._price_source.aclose()
