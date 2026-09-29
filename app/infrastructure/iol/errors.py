"""IOL adapter errors.

Every message raised here is designed to be safe for logs and for the browser:
no password, no bearer token, and no full response body is ever included.
"""

from __future__ import annotations

#: Upper bound for any message we are willing to show or log.
MAX_MESSAGE_LENGTH = 200


class IOLAPIError(Exception):
    """Base class for every IOL adapter failure."""

    #: Short, safe text that can be shown to the user.
    default_message = "The broker request failed."

    def __init__(self, message: str | None = None) -> None:
        self.message = safe_message(message or self.default_message)
        super().__init__(self.message)


class IOLAuthError(IOLAPIError):
    """The broker rejected the credentials or the token."""

    default_message = "The broker rejected the credentials."


class IOLConnectionError(IOLAPIError):
    """The broker could not be reached."""

    default_message = "The broker could not be reached."


class IOLResponseError(IOLAPIError):
    """The broker answered with an unexpected status or body."""

    default_message = "The broker returned an unexpected response."


class IOLUnavailableError(IOLAPIError):
    """The adapter cannot run at all, for example because of bad configuration."""

    default_message = "The broker adapter is not available."


def safe_message(message: str) -> str:
    """Trim and collapse a message so it stays short and single-line.

    This is a defensive formatting helper only. It does not decide what is
    secret; callers must never pass credentials or tokens in.
    """
    collapsed = " ".join(str(message).split())
    if len(collapsed) > MAX_MESSAGE_LENGTH:
        collapsed = collapsed[: MAX_MESSAGE_LENGTH - 1].rstrip() + "\u2026"
    return collapsed or "Unknown error."
