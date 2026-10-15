"""PPI adapter errors.

Every message raised here is safe for logs and for the browser: no API key, no
API secret, no bearer token and no full response body is ever included.
"""

from __future__ import annotations

from app.infrastructure.iol.errors import safe_message
from app.ports.broker_reader import BrokerError


class PPIAPIError(BrokerError):
    """Base class for every PPI adapter failure."""

    default_message = "The broker request failed."

    def __init__(self, message: str | None = None) -> None:
        self.message = safe_message(message or self.default_message)
        super().__init__(self.message)


class PPIAuthError(PPIAPIError):
    """PPI rejected the credentials or the token."""

    default_message = "The broker rejected the credentials."


class PPIConnectionError(PPIAPIError):
    """PPI could not be reached."""

    default_message = "The broker could not be reached."


class PPIResponseError(PPIAPIError):
    """PPI answered with an unexpected status or body."""

    default_message = "The broker returned an unexpected response."


class PPIUnavailableError(PPIAPIError):
    """The adapter cannot run, for example because PPI is not configured."""

    default_message = "PPI is not configured."
