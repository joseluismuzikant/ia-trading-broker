"""Minimal asynchronous IOL HTTP client.

Only the calls Day 2 needs are implemented: token, token refresh, profile, and
account status. The client never logs a request body and never puts a password
or a token into an exception message or a URL.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import get_settings
from app.infrastructure.iol.errors import (
    IOLAuthError,
    IOLConnectionError,
    IOLResponseError,
    safe_message,
)
from app.infrastructure.iol.schemas import AccountStatus, Profile, Token
from app.infrastructure.iol.tokens import CachedToken

logger = logging.getLogger("ia_trading_broker.iol")

#: Used when the broker omits or mangles ``expires_in``.
FALLBACK_TTL_SECONDS = 300


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

    # --- Internals --------------------------------------------------------

    async def _post_form(self, path: str, data: dict[str, str]) -> httpx.Response:
        try:
            response = await self._client.post(path, data=data)
        except httpx.TimeoutException as exc:
            raise IOLConnectionError("The broker did not answer in time.") from exc
        except httpx.HTTPError as exc:
            # ``httpx`` messages can quote the URL, never the form body.
            raise IOLConnectionError(
                f"The broker could not be reached ({type(exc).__name__})."
            ) from exc
        return response

    async def _get_json(self, path: str, access_token: str) -> Any:
        try:
            response = await self._client.get(
                path,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.TimeoutException as exc:
            raise IOLConnectionError("The broker did not answer in time.") from exc
        except httpx.HTTPError as exc:
            raise IOLConnectionError(
                f"The broker could not be reached ({type(exc).__name__})."
            ) from exc

        self._raise_for_status(response, context=path)

        try:
            return response.json()
        except ValueError as exc:
            raise IOLResponseError(
                f"The broker returned a non-JSON body for {path}."
            ) from exc

    def _raise_for_status(self, response: httpx.Response, *, context: str) -> None:
        """Translate an HTTP status into an adapter error."""
        status = response.status_code
        if status < 400:
            return

        if status in (401, 403):
            raise IOLAuthError(
                "The broker rejected the token. The connection may need to be re-saved."
            )

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
    def _validate(model: type[Profile] | type[AccountStatus], payload: Any, label: str):
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


def build_client() -> IOLClient:
    """Create a client from the current application settings."""
    settings = get_settings()
    return IOLClient(
        base_url=settings.iol_base_url,
        timeout=settings.iol_request_timeout_seconds,
    )
