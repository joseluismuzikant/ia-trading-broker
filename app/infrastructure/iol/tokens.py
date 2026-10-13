"""Short-lived IOL token cache.

Tokens live in memory only, keyed by broker connection. A restart simply means
the next request authenticates again, which is cheap and avoids ever writing a
bearer token to disk.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: Treat a token as expired slightly before the broker does.
DEFAULT_LEEWAY_SECONDS = 30


@dataclass(frozen=True)
class CachedToken:
    """An access token together with the moment it stops being usable."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime

    def is_expired(self, *, leeway_seconds: int = DEFAULT_LEEWAY_SECONDS) -> bool:
        """True when the token is expired or about to expire."""
        return datetime.now(UTC) >= self.expires_at - timedelta(seconds=leeway_seconds)


class TokenStore:
    """In-memory, per-connection token cache guarded by a lock."""

    def __init__(self) -> None:
        self._tokens: dict[int, CachedToken] = {}
        self._lock = asyncio.Lock()

    async def get(self, connection_id: int) -> CachedToken | None:
        """Return a usable cached token, or None."""
        async with self._lock:
            token = self._tokens.get(connection_id)
        if token is None or token.is_expired():
            return None
        return token

    async def set(self, connection_id: int, token: CachedToken) -> None:
        """Store a freshly issued token."""
        async with self._lock:
            self._tokens[connection_id] = token

    async def discard(self, connection_id: int) -> None:
        """Forget the token for one connection."""
        async with self._lock:
            self._tokens.pop(connection_id, None)

    async def clear(self) -> None:
        """Forget every cached token."""
        async with self._lock:
            self._tokens.clear()


#: Process-wide store. Tests replace it with a fresh instance.
token_store = TokenStore()
