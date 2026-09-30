"""Test doubles for the IOL HTTP API.

``FakeIOL`` is an ``httpx`` transport that answers the read endpoints from saved
fixtures. It records what was requested so tests can assert on token grants and
call counts. It contains no real credentials or account data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "iol"

PROFILE_PATH = "/api/v2/datos-perfil"
ACCOUNT_STATUS_PATH = "/api/v2/estadocuenta"
PORTFOLIO_PATH_PREFIX = "/api/v2/portafolio/"
QUOTE_PATH_MARKER = "/Cotizacion"
HISTORY_PATH_MARKER = "/Cotizacion/seriehistorica/"


def load_fixture(name: str) -> Any:
    """Read one saved JSON fixture."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@dataclass
class FakeIOL:
    """A scriptable stand-in for the IOL REST API."""

    username: str = "iol-test-user"
    password: str = "iol-test-password"
    token_ttl: int = 899

    profile: dict = field(default_factory=lambda: load_fixture("profile_ok.json"))
    account_status: dict = field(
        default_factory=lambda: load_fixture("account_status_ok.json")
    )
    portfolio: dict = field(default_factory=lambda: load_fixture("portfolio_ok.json"))
    quote: dict = field(
        default_factory=lambda: {
            "ultimoPrecio": 60.555,
            "variacion": 0.5,
            "moneda": "Peso_Argentino",
            "fechaHora": "2024-01-02T15:00:00",
        }
    )
    price_history: list[dict] = field(
        default_factory=lambda: [
            {"ultimoPrecio": 60.555, "fechaHora": "2024-01-02T00:00:00"},
            {"ultimoPrecio": 59.0, "fechaHora": "2023-12-29T00:00:00"},
        ]
    )

    #: Force an error status for one endpoint instead of returning data.
    profile_error_status: int | None = None
    account_status_error_status: int | None = None
    portfolio_error_status: int | None = None
    quote_error_status: int | None = None

    #: Reject this many authenticated requests before accepting one, which
    #: simulates a token that expired or was revoked server-side.
    reject_authenticated_requests: int = 0
    #: When False, the refresh-token grant fails, forcing a fresh login.
    accept_refresh_tokens: bool = True

    # --- Recorded behaviour ----------------------------------------------
    token_grants: list[str] = field(default_factory=list)
    authenticated_paths: list[str] = field(default_factory=list)
    issued_tokens: list[str] = field(default_factory=list)

    _valid_tokens: set[str] = field(default_factory=set)
    _counter: int = 0

    # --- Transport --------------------------------------------------------

    @property
    def transport(self) -> httpx.MockTransport:
        """An httpx transport wired to this fake."""
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return self._handle_token(request)
        return self._handle_authenticated(request)

    def _issue_token(self) -> dict:
        self._counter += 1
        token = f"access-token-{self._counter}"
        self.issued_tokens.append(token)
        self._valid_tokens.add(token)
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": self.token_ttl,
            "refresh_token": f"refresh-token-{self._counter}",
        }

    def _handle_token(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("utf-8"))
        grant = (form.get("grant_type") or [""])[0]
        self.token_grants.append(grant)

        if grant == "password":
            given_user = (form.get("username") or [""])[0]
            given_password = (form.get("password") or [""])[0]
            if given_user != self.username or given_password != self.password:
                return httpx.Response(
                    400, json=load_fixture("token_invalid_grant.json")
                )
            return httpx.Response(200, json=self._issue_token())

        if grant == "refresh_token":
            if not self.accept_refresh_tokens:
                return httpx.Response(
                    400, json=load_fixture("token_invalid_grant.json")
                )
            return httpx.Response(200, json=self._issue_token())

        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _handle_authenticated(self, request: httpx.Request) -> httpx.Response:
        self.authenticated_paths.append(request.url.path)

        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        if token not in self._valid_tokens:
            return httpx.Response(401, json={"error": "unauthorized"})

        if self.reject_authenticated_requests > 0:
            self.reject_authenticated_requests -= 1
            # The token is now unusable, exactly like an expired one.
            self._valid_tokens.discard(token)
            return httpx.Response(401, json={"error": "unauthorized"})

        if request.url.path == PROFILE_PATH:
            if self.profile_error_status:
                return httpx.Response(self.profile_error_status, json={"error": "boom"})
            return httpx.Response(200, json=self.profile)

        if request.url.path == ACCOUNT_STATUS_PATH:
            if self.account_status_error_status:
                return httpx.Response(
                    self.account_status_error_status, json={"error": "boom"}
                )
            return httpx.Response(200, json=self.account_status)

        if request.url.path.startswith(PORTFOLIO_PATH_PREFIX):
            if self.portfolio_error_status:
                return httpx.Response(
                    self.portfolio_error_status, json={"error": "boom"}
                )
            return httpx.Response(200, json=self.portfolio)

        # The history path also contains the quote marker, so check it first.
        if HISTORY_PATH_MARKER in request.url.path:
            if self.quote_error_status:
                return httpx.Response(self.quote_error_status, json={"error": "boom"})
            return httpx.Response(200, json=self.price_history)

        if QUOTE_PATH_MARKER in request.url.path:
            if self.quote_error_status:
                return httpx.Response(self.quote_error_status, json={"error": "boom"})
            return httpx.Response(200, json=self.quote)

        return httpx.Response(404, json={"error": "not found"})
