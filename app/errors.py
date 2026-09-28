"""Application-specific exceptions."""

from __future__ import annotations


class RedirectToLogin(Exception):
    """Raised when a protected page is opened without a valid session.

    Handled by an exception handler that returns a 303 redirect to the login
    page instead of a JSON error body.
    """

    def __init__(self, next_url: str = "/login") -> None:
        self.next_url = next_url
        super().__init__("authentication required")


class CsrfError(Exception):
    """Raised when a submitted form fails CSRF validation."""

    def __init__(self, message: str = "invalid or missing CSRF token") -> None:
        self.message = message
        super().__init__(message)
