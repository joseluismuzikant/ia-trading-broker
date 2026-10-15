"""PPI as a :class:`~app.ports.broker_reader.BrokerReader`, as far as PPI documents.

What PPI's API documents is read: the login (public and private API keys), the
cash balances, daily prices for an instrument, and the detail of an order.

What it does not document is *positions*. PPI's published schema for
``BalancesAndPositions`` has no list of holdings, so :meth:`get_portfolio`
refuses instead of guessing, and a PPI connection cannot yet run an analysis.
Order sending is not wired either (``can_trade`` is false for PPI).

Only an instrument type PPI is known to accept is queried, and only the cash
PPI reports for immediate settlement counts as available.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

from app.domain.money import normalise_currency
from app.domain.portfolio import Portfolio
from app.infrastructure.iol.tokens import CachedToken, token_store
from app.infrastructure.ppi.client import PPIClient
from app.infrastructure.ppi.errors import PPIAuthError
from app.infrastructure.ppi.schemas import PPIToken
from app.ports.broker_reader import BrokerUnavailableError, CashBalance, PriceBar

logger = logging.getLogger("ia_trading_broker.ppi.broker")

T = TypeVar("T")

#: Settlement used when reading cash and prices. PPI's Postman collection uses it
#: for budgets; any other term is not counted as available cash.
READ_SETTLEMENT = "INMEDIATA"

#: How long a token is trusted when PPI does not say.
DEFAULT_TOKEN_SECONDS = 600


def _cached_from(token: PPIToken) -> CachedToken:
    expires_at = token.expiration_date or (
        datetime.now(UTC) + timedelta(seconds=DEFAULT_TOKEN_SECONDS)
    )
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return CachedToken(
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        expires_at=expires_at,
    )


class PPIBroker:
    """Read one PPI account. Owns authentication; sends no order."""

    def __init__(
        self,
        *,
        connection_id: int,
        account_number: str,
        credentials_provider: Callable[[], tuple[str, str]],
        client: PPIClient,
        instrument_type_for: Callable[[str], str | None],
    ) -> None:
        self._connection_id = connection_id
        self._account = account_number
        # (public key, private key), decrypted only when a login is needed.
        self._credentials_provider = credentials_provider
        self._client = client
        self._instrument_type_for = instrument_type_for

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _login(self, previous: CachedToken | None = None) -> CachedToken:
        if previous is not None and previous.refresh_token:
            try:
                renewed = await self._client.refresh(
                    access_token=previous.access_token, refresh_token=previous.refresh_token
                )
            except PPIAuthError:
                logger.info(
                    "refresh failed for connection id=%s; falling back to a login",
                    self._connection_id,
                )
            else:
                cached = _cached_from(renewed)
                await token_store.set(self._connection_id, cached)
                return cached
        api_key, api_secret = self._credentials_provider()
        cached = _cached_from(await self._client.login(api_key=api_key, api_secret=api_secret))
        await token_store.set(self._connection_id, cached)
        return cached

    async def _call(self, call: Callable[[str], Awaitable[T]]) -> T:
        """Run ``call`` with a valid token, renewing exactly once after a rejection."""
        cached = await token_store.get(self._connection_id)
        token = cached if cached is not None else await self._login()
        try:
            return await call(token.access_token)
        except PPIAuthError:
            logger.info("token rejected for connection id=%s; renewing once", self._connection_id)
            await token_store.discard(self._connection_id)
            renewed = await self._login(previous=token)
            return await call(renewed.access_token)

    # --- BrokerReader ----------------------------------------------------

    async def authenticate(self) -> None:
        async def proves_a_token(_token: str) -> None:
            return None

        await self._call(proves_a_token)

    async def get_portfolio(self, country: str) -> Portfolio:
        raise BrokerUnavailableError(
            "PPI's published API does not describe a list of holdings, so a PPI "
            "portfolio cannot be read yet. Cash and prices work."
        )

    async def get_balances(self) -> list[CashBalance]:
        lines = await self._call(lambda token: self._client.available_balance(token, self._account))
        totals: dict[str | None, Decimal] = {}
        for line in lines:
            if line.settlement.strip().upper() != READ_SETTLEMENT:
                continue  # money that settles later is not available to spend now
            currency = normalise_currency(line.simbol)
            totals[currency] = totals.get(currency, Decimal("0")) + line.amount
        return [CashBalance(currency=c, available=total) for c, total in totals.items()]

    async def get_history(self, *, symbol: str, market: str, days: int) -> list[PriceBar]:
        clean = symbol.strip().upper()
        instrument_type = self._instrument_type_for(clean)
        if not instrument_type:
            raise BrokerUnavailableError(
                f"PPI's instrument type for {clean} is not confirmed, so its prices are not read."
            )
        date_to = datetime.now(UTC).date()
        quotes = await self._call(
            lambda token: self._client.price_history(
                token,
                ticker=clean,
                instrument_type=instrument_type,
                settlement=READ_SETTLEMENT,
                date_from=date_to - timedelta(days=days),
                date_to=date_to,
            )
        )
        return [
            PriceBar(
                at=quote.date.isoformat() if quote.date else None,
                close=float(quote.price),
                high=float(quote.max) if quote.max is not None else None,
                low=float(quote.min) if quote.min is not None else None,
                volume=float(quote.volume) if quote.volume is not None else None,
            )
            for quote in quotes
        ]

    async def get_operation(self, broker_order_id: str) -> dict[str, Any]:
        document = await self._call(
            lambda token: self._client.order_detail(
                token, account_number=self._account, order_id=broker_order_id
            )
        )
        return document.model_dump(mode="json", by_alias=True)
