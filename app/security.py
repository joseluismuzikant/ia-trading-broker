"""Password hashing, token generation, and constant-time comparison helpers.

Passwords are hashed with Argon2id. Session tokens and CSRF tokens are random
values; only a hash of the session token is stored in the database.
"""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2 import Type as Argon2Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

#: Argon2id parameters. The library picks sensible defaults; the type is set
#: explicitly because the project requires Argon2id.
_password_hasher = PasswordHasher(type=Argon2Type.ID)

#: Number of random bytes used for session and CSRF tokens.
TOKEN_BYTES = 32


def hash_password(password: str) -> str:
    """Return an Argon2id hash for the given plaintext password."""
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return True when the password matches the stored hash."""
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """Return True when the stored hash should be recomputed."""
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def generate_token() -> str:
    """Return a new URL-safe random token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest used to store a session token."""
    if not token:
        raise ValueError("token must not be empty")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(left: str, right: str) -> bool:
    """Compare two tokens without leaking timing information."""
    if not left or not right:
        return False
    return secrets.compare_digest(left, right)
