"""HTTP client for the PPI REST API (``/api/1.0``).

The request shapes follow PPI's own Postman collection (``Client API -
Clientes PPI``, kept locally in the git-ignored ``postman/`` folder): the integration headers
``AuthorizedClient`` and ``ClientKey`` on every call, ``ApiKey`` and
``ApiSecret`` on the login only, and ``Authorization: Bearer`` afterwards. The
response shapes come from ``ppi_rest_openapi_with_examples.json`` (also in ``postman/``), whose
own description says its examples are illustrative rather than recorded, so
nothing here is trusted beyond what is validated.

Money safety, as for IOL:

* a trading POST is sent once and never retried inside this class;
* the answer to ``Order/Confirm`` is classified strictly. A ``200`` carrying an
  order id is ``accepted`` (created, not necessarily filled); a clear 4xx is
  ``rejected``; a token refusal (401/403) proves nothing was placed; a timeout,
  a transport failure, a 5xx, or a ``200`` without an order id is ``unknown``
  and must be reconciled, never resent;
* the API secret and tokens are never stored on the client, logged, or put in
  an error message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.infrastructure.iol.errors import safe_message
from app.infrastructure.ppi.errors import (
    PPIAuthError,
    PPIConnectionError,
    PPIResponseError,
    PPIUnavailableError,
)
from app.infrastructure.ppi.schemas import (
    PPIAccount,
    PPIBalance,
    PPIOrderDocument,
    PPIQuote,
    PPIToken,
)

logger = logging.getLogger("ia_trading_broker.ppi")

TRADE_ACCEPTED = "accepted"
TRADE_REJECTED = "rejected"
TRADE_UNKNOWN = "unknown"
TRADE_AUTH = "auth"

M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True)
class TradeAck:
    """What PPI said about one ``Order/Confirm``, strictly classified."""

    status: str
    order_id: str | None = None
    message: str = ""

    @property
    def accepted(self) -> bool:
        return self.status == TRADE_ACCEPTED


class PPIClient:
    """Async client. Create one per operation or per connection and close it."""

    def __init__(
        self,
        *,
        base_url: str,
        authorized_client: str,
        client_key: str,
        timeout: float = 15.0,
        version: str = "1.0",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not authorized_client.strip() or not client_key.strip():
            raise PPIUnavailableError(
                "PPI_AUTHORIZED_CLIENT and PPI_CLIENT_KEY must be set to use PPI."
            )
        self._prefix = f"/api/{version}"
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "AuthorizedClient": authorized_client,
                "ClientKey": client_key,
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> PPIClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Session ---------------------------------------------------------

    async def login(self, *, api_key: str, api_secret: str) -> PPIToken:
        """Exchange the user's public and private API keys for a session."""
        response = await self._send(
            "POST",
            "/Account/LoginApi",
            headers={"ApiKey": api_key, "ApiSecret": api_secret},
        )
        self._raise_for_status(response, context="login")
        return self._parse_token(response)

    async def refresh(self, *, access_token: str, refresh_token: str) -> PPIToken:
        """Renew a session from its refresh token."""
        response = await self._send(
            "POST",
            "/Account/RefreshToken",
            token=access_token,
            json={"refreshToken": refresh_token},
        )
        self._raise_for_status(response, context="token refresh")
        return self._parse_token(response)

    # --- Reads -----------------------------------------------------------

    async def accounts(self, token: str) -> list[PPIAccount]:
        return await self._get_list(token, "/Account/Accounts", PPIAccount, "accounts")

    async def available_balance(self, token: str, account_number: str) -> list[PPIBalance]:
        return await self._get_list(
            token,
            "/Account/AvailableBalance",
            PPIBalance,
            "available balance",
            params={"accountNumber": account_number},
        )

    async def current_quote(
        self, token: str, *, ticker: str, instrument_type: str, settlement: str
    ) -> PPIQuote:
        payload = await self._get_json(
            token,
            "/MarketData/Current",
            "current quote",
            params={"Ticker": ticker, "Type": instrument_type, "Settlement": settlement},
        )
        return self._validate(PPIQuote, payload, "current quote")

    async def price_history(
        self,
        token: str,
        *,
        ticker: str,
        instrument_type: str,
        settlement: str,
        date_from: date,
        date_to: date,
    ) -> list[PPIQuote]:
        return await self._get_list(
            token,
            "/MarketData/Search",
            PPIQuote,
            "price history",
            params={
                "Ticker": ticker,
                "Type": instrument_type,
                "Settlement": settlement,
                "DateFrom": f"{date_from.isoformat()}T00:00:00Z",
                "DateTo": f"{date_to.isoformat()}T00:00:00Z",
            },
        )

    async def order_detail(
        self,
        token: str,
        *,
        account_number: str,
        order_id: str | None = None,
        external_id: str | None = None,
    ) -> PPIOrderDocument:
        """One order, by PPI's id or by the id this application assigned."""
        params: dict[str, str] = {"accountNumber": account_number}
        if order_id:
            params["orderID"] = order_id
        if external_id:
            params["externalID"] = external_id
        payload = await self._get_json(token, "/Order/Detail", "order detail", params=params)
        return self._validate(PPIOrderDocument, payload, "order detail")

    # --- Orders ----------------------------------------------------------

    async def budget(self, token: str, request: dict[str, Any]) -> PPIOrderDocument:
        """Price an order and list its disclaimers. Places nothing."""
        response = await self._send("POST", "/Order/Budget", token=token, json=request)
        self._raise_for_status(response, context="order budget")
        return self._validate(PPIOrderDocument, _json(response), "order budget")

    async def confirm(self, token: str, request: dict[str, Any]) -> TradeAck:
        """Create the order. Sent once, never retried, strictly classified."""
        try:
            response = await self._send("POST", "/Order/Confirm", token=token, json=request)
        except PPIConnectionError as exc:
            # The request may have reached PPI before the connection broke.
            logger.warning("Order/Confirm failed in transport")
            return TradeAck(status=TRADE_UNKNOWN, message=exc.message)
        return self._trade_ack(response)

    # --- Internals -------------------------------------------------------

    async def _send(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        merged = dict(headers or {})
        if token:
            merged["Authorization"] = f"Bearer {token}"
        try:
            return await self._client.request(
                method, f"{self._prefix}{path}", headers=merged, **kwargs
            )
        except httpx.HTTPError as exc:
            raise PPIConnectionError(_transport_message(exc)) from exc

    async def _get_json(
        self, token: str, path: str, context: str, *, params: dict[str, str] | None = None
    ) -> Any:
        response = await self._send("GET", path, token=token, params=params)
        self._raise_for_status(response, context=context)
        return _json(response)

    async def _get_list(
        self,
        token: str,
        path: str,
        model: type[M],
        context: str,
        *,
        params: dict[str, str] | None = None,
    ) -> list[M]:
        payload = await self._get_json(token, path, context, params=params)
        if not isinstance(payload, list):
            raise PPIResponseError(f"PPI {context} was not a list.")
        return [self._validate(model, item, context) for item in payload]

    def _trade_ack(self, response: httpx.Response) -> TradeAck:
        status = response.status_code
        if status in (401, 403):
            return TradeAck(status=TRADE_AUTH, message="The token was not accepted.")
        if 200 <= status < 300:
            try:
                document = PPIOrderDocument.model_validate(_json(response))
            except (PPIResponseError, ValidationError):
                document = None
            if document is not None and document.id is not None:
                return TradeAck(status=TRADE_ACCEPTED, order_id=str(document.id))
            # Success without an order id proves nothing about the order.
            return TradeAck(
                status=TRADE_UNKNOWN,
                message="PPI answered without an order id; the order must be reconciled.",
            )
        if 400 <= status < 500:
            return TradeAck(
                status=TRADE_REJECTED,
                message=_error_detail(response) or "PPI refused the order.",
            )
        return TradeAck(
            status=TRADE_UNKNOWN,
            message=f"PPI answered HTTP {status}; the order must be reconciled.",
        )

    def _raise_for_status(self, response: httpx.Response, *, context: str) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if status in (401, 403):
            raise PPIAuthError(f"PPI refused the {context}: the session or keys are not valid.")
        detail = _error_detail(response)
        raise PPIResponseError(f"PPI {context} failed (HTTP {status}). {detail}".strip())

    def _parse_token(self, response: httpx.Response) -> PPIToken:
        payload = _json(response)
        # The OpenAPI reconstruction shows a list, the Postman script an object.
        if isinstance(payload, list) and len(payload) == 1:
            payload = payload[0]
        token = self._validate(PPIToken, payload, "login")
        if token.token_type.lower() != "bearer":
            raise PPIResponseError("PPI returned a token type this adapter does not support.")
        return token

    @staticmethod
    def _validate(model: type[M], payload: Any, label: str) -> M:
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            # Field names only: the values may hold account data.
            fields = ", ".join(sorted({str(err["loc"][0]) for err in exc.errors() if err["loc"]}))
            raise PPIResponseError(f"PPI {label} had an unexpected shape ({fields}).") from exc


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise PPIResponseError("PPI returned a body that is not JSON.") from exc


def _error_detail(response: httpx.Response) -> str:
    """PPI's own short error text, or an empty string."""
    try:
        payload = response.json()
    except ValueError:
        return ""
    if isinstance(payload, dict):
        for key in ("message", "error_description", "error", "title", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return safe_message(value)
    return ""


def _transport_message(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "The broker did not answer in time."
    return f"The broker could not be reached ({type(exc).__name__})."


def build_client() -> PPIClient:
    """Create a client from the application settings."""
    settings = get_settings()
    authorized = settings.ppi_authorized_client
    key = settings.ppi_client_key
    return PPIClient(
        base_url=settings.ppi_base_url,
        authorized_client=authorized.get_secret_value() if authorized else "",
        client_key=key.get_secret_value() if key else "",
        timeout=settings.ppi_request_timeout_seconds,
    )
