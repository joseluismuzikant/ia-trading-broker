"""InvertirOnline (IOL) adapter.

Everything specific to the IOL REST API lives in this package. The rest of the
application only sees the typed models and the errors defined here.
"""

from app.infrastructure.iol.client import IOLClient, build_client
from app.infrastructure.iol.errors import (
    IOLAPIError,
    IOLAuthError,
    IOLConnectionError,
    IOLResponseError,
    IOLUnavailableError,
    safe_message,
)
from app.infrastructure.iol.schemas import AccountStatus, Profile, Token

__all__ = [
    "AccountStatus",
    "IOLAPIError",
    "IOLAuthError",
    "IOLClient",
    "IOLConnectionError",
    "IOLResponseError",
    "IOLUnavailableError",
    "Profile",
    "Token",
    "build_client",
    "safe_message",
]
