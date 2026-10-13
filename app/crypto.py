"""Symmetric encryption for stored broker credentials.

IOL passwords are only ever kept in this encrypted form. The Fernet key comes
from the ``CREDENTIAL_ENCRYPTION_KEY`` setting and never from the database, so a
database dump alone does not expose any broker password.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class CredentialEncryptionError(Exception):
    """Raised when a credential cannot be encrypted or decrypted."""


@lru_cache
def _build_fernet() -> Fernet:
    key = get_settings().credential_encryption_key.get_secret_value()
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise CredentialEncryptionError(
            "CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key"
        ) from exc


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret and return the opaque token to store."""
    if not plaintext:
        raise CredentialEncryptionError("refusing to encrypt an empty secret")
    return _build_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored token.

    Raises :class:`CredentialEncryptionError` when the key changed or the value
    was tampered with. The message never includes the ciphertext.
    """
    if not ciphertext:
        raise CredentialEncryptionError("no encrypted credential to decrypt")
    try:
        return _build_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise CredentialEncryptionError(
            "stored credential cannot be decrypted with the current key"
        ) from exc


def reset_fernet_cache() -> None:
    """Drop the cached Fernet instance (used by tests after settings change)."""
    _build_fernet.cache_clear()
