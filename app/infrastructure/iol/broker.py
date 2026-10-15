"""IOL as a :class:`~app.ports.broker_reader.BrokerReader`.

Everything about reading IOL lives here: the password login, the in-memory
token cache, and the one renewal after a rejected token. A service asks for a
portfolio or a price history and gets the application's own types back; it never
sees a token, an IOL field name, or the retry rule.

Reads may be retried once. The order executor is deliberately separate: a write
is never retried by this class.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

from app.domain.money import normalise_currency
from app.domain.portfolio import Portfolio
from app.infrastructure.iol.client import IOLClient
from app.infrastructure.iol.errors import IOLAPIError, IOLAuthError
from app.infrastructure.iol.mapping import to_portfolio
from app.infrastructure.iol.schemas import AccountStatus, Quote
from app.infrastructure.iol.tokens import CachedToken, token_store
from app.ports.broker_reader import CashBalance, PriceBar

logger = logging.getLogger("ia_trading_broker.iol.broker")

T = TypeVar("T")


def price_bar_from_quote(quote: Quote) -> PriceBar:
    """One IOL quote row as an application price bar."""
    return PriceBar(
        at=quote.fecha_hora,
        close=quote.ultimo_precio,
        high=quote.maximo,
        low=quote.minimo,
        volume=quote.volumen_nominal,
    )


class IOLBroker:
    """Read one IOL account. Owns authentication; sends no order."""

    def __init__(
        self,
        *,
        connection_id: int,
        username: str,
        password_provider: Callable[[], str],
        client: IOLClient,
    ) -> None:
        self._connection_id = connection_id
        self._username = username
        # The password is only decrypted when a login is actually needed.
        self._password_provider = password_provider
        self._client = client

    # --- Session ---------------------------------------------------------

    @property
    def client(self) -> IOLClient:
        """The HTTP client, for the trading session that owns the writes."""
        return self._client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _login(self, refresh_token: str | None = None) -> CachedToken:
        """A usable token: refreshed when possible, a fresh login otherwise."""
        if refresh_token:
            try:
                token = await self._client.refresh_token(refresh_token)
            except IOLAPIError:
                logger.info(
                    "refresh failed for connection id=%s; falling back to a login",
                    self._connection_id,
                )
            else:
                await token_store.set(self._connection_id, token)
                return token
        token = await self._client.fetch_token(
            username=self._username, password=self._password_provider()
        )
        await token_store.set(self._connection_id, token)
        return token

    async def access_token(self) -> str:
        """The cached access token, logging in when there is none."""
        cached = await token_store.get(self._connection_id)
        token = cached if cached is not None else await self._login()
        return token.access_token

    async def renew_access_token(self) -> str:
        """Discard the cached token and log in again.

        Only for a provable refusal (401/403), where the broker placed nothing.
        """
        await token_store.discard(self._connection_id)
        return (await self._login()).access_token

    async def _call(self, call: Callable[[str], Awaitable[T]]) -> T:
        """Run ``call`` with a valid token, renewing exactly once after a rejection."""
        cached = await token_store.get(self._connection_id)
        token = cached if cached is not None else await self._login()
        try:
            return await call(token.access_token)
        except IOLAuthError:
            logger.info("token rejected for connection id=%s; renewing once", self._connection_id)
            await token_store.discard(self._connection_id)
            renewed = await self._login(refresh_token=token.refresh_token)
            return await call(renewed.access_token)

    # --- BrokerReader ----------------------------------------------------

    async def authenticate(self) -> None:
        async def proves_a_token(_token: str) -> None:
            return None

        await self._call(proves_a_token)

    async def get_portfolio(self, country: str) -> Portfolio:
        raw = await self._call(lambda token: self._client.get_portfolio(token, country))
        return to_portfolio(country, raw)

    async def get_balances(self) -> list[CashBalance]:
        status = await self._call(self._client.get_account_status)
        return balances_from_status(status)

    async def get_history(self, *, symbol: str, market: str, days: int) -> list[PriceBar]:
        date_to = datetime.now(UTC).date()
        date_from = date_to - timedelta(days=days)
        quotes = await self._call(
            lambda token: self._client.get_price_history(
                token,
                market=market.strip().upper(),
                symbol=symbol.strip().upper(),
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
            )
        )
        return [price_bar_from_quote(quote) for quote in quotes]

    async def get_operation(self, broker_order_id: str) -> dict[str, Any]:
        return await self._call(lambda token: self._client.get_operation(token, broker_order_id))

    # --- IOL-only reads (the profile and account pages show IOL's own shape) ---

    async def get_profile_payload(self) -> dict[str, Any]:
        profile = await self._call(self._client.get_profile)
        return profile.model_dump(mode="json", by_alias=True)

    async def get_account_status_payload(self) -> dict[str, Any]:
        status = await self._call(self._client.get_account_status)
        return status.model_dump(mode="json", by_alias=True)


def balances_from_status(status: AccountStatus) -> list[CashBalance]:
    """Free cash per currency from an IOL account status.

    Accounts in the same currency are added together, and an account that does
    not report its available cash is skipped rather than counted as zero.
    """
    totals: dict[str | None, Decimal] = {}
    for account in status.cuentas:
        if account.disponible is None:
            continue
        currency = normalise_currency(account.moneda)
        totals[currency] = totals.get(currency, Decimal("0")) + Decimal(str(account.disponible))
    return [CashBalance(currency=currency, available=total) for currency, total in totals.items()]
