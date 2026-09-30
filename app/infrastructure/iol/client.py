"""Minimal asynchronous IOL HTTP client.

Only the calls the read-only days need are implemented: token, token refresh,
profile, account status, country portfolio, quotes, and price history. The
client never logs a request body and never puts a password or a token into an
exception message or a URL.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.config import get_settings
from app.infrastructure.iol.budget import call_budget
from app.infrastructure.iol.errors import (
    IOLAPIError,
    IOLAuthError,
    IOLConnectionError,
    IOLNotFoundError,
    IOLResponseError,
    safe_message,
)
from app.infrastructure.iol.schemas import (
    AccountStatus,
    IOLPortfolio,
    Profile,
    Quote,
    Token,
)
from app.infrastructure.iol.tokens import CachedToken

logger = logging.getLogger("ia_trading_broker.iol")

#: Used when the broker omits or mangles ``expires_in``.
FALLBACK_TTL_SECONDS = 300

#: Extra attempts for safe (GET) reads before giving up. Only idempotent reads
#: are repeated this way; a write is never retried automatically.
SAFE_READ_RETRIES = 2


class IOLClient:
    """A thin, credential-aware wrapper around the IOL REST API."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def __aenter__(self) -> IOLClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._client.aclose()

    # --- Authentication ---------------------------------------------------

    async def fetch_token(self, *, username: str, password: str) -> CachedToken:
        """Request a new token with the password grant."""
        response = await self._post_form(
            "/token",
            {
                "grant_type": "password",
                "username": username,
                "password": password,
            },
        )
        return self._parse_token(response)

    async def refresh_token(self, refresh_token: str) -> CachedToken:
        """Exchange a refresh token for a new access token."""
        response = await self._post_form(
            "/token",
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        return self._parse_token(response)

    # --- Account reads ----------------------------------------------------

    async def get_profile(self, access_token: str) -> Profile:
        """Read the authenticated user's profile."""
        payload = await self._get_json("/api/v2/datos-perfil", access_token)
        return self._validate(Profile, payload, "profile")

    async def get_account_status(self, access_token: str) -> AccountStatus:
        """Read the authenticated user's account status."""
        payload = await self._get_json("/api/v2/estadocuenta", access_token)
        return self._validate(AccountStatus, payload, "account status")

    # --- Portfolio and market reads ---------------------------------------

    async def get_portfolio(self, access_token: str, country: str) -> IOLPortfolio:
        """Read the authenticated user's portfolio for one country."""
        path = f"/api/v2/portafolio/{quote(country, safe='')}"
        payload = await self._get_json(path, access_token)
        return self._validate(IOLPortfolio, payload, "portfolio")

    async def get_quote(
        self,
        access_token: str,
        *,
        market: str,
        symbol: str,
        plazo: str | None = None,
    ) -> Quote:
        """Read the latest quote for one instrument in one market."""
        params = {"plazo": plazo} if plazo else None
        payload = await self._get_json(
            _quote_path(market, symbol), access_token, params=params
        )
        return self._validate(Quote, payload, "quote")

    async def get_price_history(
        self,
        access_token: str,
        *,
        market: str,
        symbol: str,
        date_from: str,
        date_to: str,
        adjusted: bool = False,
    ) -> list[Quote]:
        """Read the historical quotes for one instrument between two dates."""
        flag = "true" if adjusted else "false"
        path = (
            f"{_quote_path(market, symbol)}/seriehistorica/"
            f"{quote(date_from, safe='')}/{quote(date_to, safe='')}/{flag}"
        )
        payload = await self._get_json(path, access_token)
        if not isinstance(payload, list):
            raise IOLResponseError("The broker price history was not a list.")
        return [self._validate(Quote, item, "price history") for item in payload]

    # --- Internals --------------------------------------------------------

    async def _post_form(self, path: str, data: dict[str, str]) -> httpx.Response:
        # Counted before sending: a failed attempt still costs a call.
        call_budget.check()
        call_budget.record()
        try:
            response = await self._client.post(path, data=data)
        except httpx.HTTPError as exc:
            # A POST is not safe to repeat blindly, so it is never retried.
            # ``httpx`` messages can quote the URL, never the form body.
            raise IOLConnectionError(_transport_message(exc)) from exc
        return response

    async def _get_json(
        self,
        path: str,
        access_token: str,
        *,
        params: dict[str, str] | None = None,
    ) -> Any:
        """Read a JSON body, retrying a safe GET on a transient failure."""
        last_error: IOLAPIError | None = None

        for attempt in range(SAFE_READ_RETRIES + 1):
            final = attempt == SAFE_READ_RETRIES
            # Every attempt is one billable call, so the budget is checked
            # before each one, including the retries.
            call_budget.check()
            call_budget.record()
            try:
                response = await self._client.get(
                    path,
                    params=params,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except httpx.HTTPError as exc:
                last_error = IOLConnectionError(_transport_message(exc))
                if final:
                    raise last_error from exc
                continue

            if response.status_code >= 500 and not final:
                # A server error may be transient, and a GET is safe to
                # repeat, so give the broker one more chance before failing.
                last_error = IOLResponseError(
                    f"The broker answered {response.status_code} for {path}."
                )
                continue

            self._raise_for_status(response, context=path)
            try:
                return response.json()
            except ValueError as exc:
                raise IOLResponseError(
                    f"The broker returned a non-JSON body for {path}."
                ) from exc

        # Every branch above returns or raises; this is only a safety net.
        assert last_error is not None  # pragma: no cover - defensive
        raise last_error

    def _raise_for_status(self, response: httpx.Response, *, context: str) -> None:
        """Translate an HTTP status into an adapter error."""
        status = response.status_code
        if status < 400:
            return

        if status in (401, 403):
            raise IOLAuthError(
                "The broker rejected the token. The connection may need to be re-saved."
            )

        if status == 404:
            raise IOLNotFoundError(f"The broker does not know {context}.")

        detail = _error_detail(response)
        if status == 400 and detail:
            # ``invalid_grant`` means the username or password is wrong.
            raise IOLAuthError(detail)

        raise IOLResponseError(f"The broker answered {status} for {context}. {detail}".strip())

    def _parse_token(self, response: httpx.Response) -> CachedToken:
        if response.status_code >= 400:
            detail = _error_detail(response)
            if response.status_code in (400, 401, 403):
                raise IOLAuthError(detail or "The broker rejected the credentials.")
            self._raise_for_status(response, context="/token")

        try:
            token = Token.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise IOLResponseError("The broker returned an unexpected token body.") from exc

        if not token.access_token:
            raise IOLResponseError("The broker returned an empty access token.")

        ttl = token.expires_in if token.expires_in and token.expires_in > 0 else FALLBACK_TTL_SECONDS
        return CachedToken(
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        )

    @staticmethod
    def _validate(model: type[Any], payload: Any, label: str) -> Any:
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            # Report the field names only, never the received values.
            fields = ", ".join(str(error["loc"][0]) for error in exc.errors() if error.get("loc"))
            raise IOLResponseError(
                f"The broker {label} response did not match the expected shape ({fields})."
            ) from exc


def _error_detail(response: httpx.Response) -> str:
    """Return the broker's own short error text, or an empty string."""
    try:
        payload = response.json()
    except ValueError:
        return ""

    if isinstance(payload, dict):
        for key in ("error_description", "error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return safe_message(value)
    return ""


def _transport_message(exc: httpx.HTTPError) -> str:
    """Safe one-line text for a transport failure."""
    if isinstance(exc, httpx.TimeoutException):
        return "The broker did not answer in time."
    return f"The broker could not be reached ({type(exc).__name__})."


def _quote_path(market: str, symbol: str) -> str:
    """Build the quote path with the market and symbol percent-encoded."""
    return f"/api/v2/{quote(market, safe='')}/Titulos/{quote(symbol, safe='')}/Cotizacion"


def build_client() -> IOLClient:
    """Create a client from the current application settings."""
    settings = get_settings()
    return IOLClient(
        base_url=settings.iol_base_url,
        timeout=settings.iol_request_timeout_seconds,
    )
