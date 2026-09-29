"""Tests for the credential encryption layer."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.crypto import (
    CredentialEncryptionError,
    decrypt_secret,
    encrypt_secret,
)


def test_roundtrip_returns_the_original_secret() -> None:
    ciphertext = encrypt_secret("a-very-secret-password")
    assert decrypt_secret(ciphertext) == "a-very-secret-password"


def test_ciphertext_does_not_contain_the_plaintext() -> None:
    ciphertext = encrypt_secret("a-very-secret-password")
    assert "a-very-secret-password" not in ciphertext
    assert ciphertext != "a-very-secret-password"


def test_same_secret_encrypts_to_different_ciphertexts() -> None:
    assert encrypt_secret("repeatable") != encrypt_secret("repeatable")


def test_empty_secret_is_rejected() -> None:
    with pytest.raises(CredentialEncryptionError):
        encrypt_secret("")


def test_decrypting_garbage_raises_a_safe_error() -> None:
    with pytest.raises(CredentialEncryptionError) as error:
        decrypt_secret("not-a-real-fernet-token")

    assert "not-a-real-fernet-token" not in str(error.value)


def test_decrypting_an_empty_value_raises() -> None:
    with pytest.raises(CredentialEncryptionError):
        decrypt_secret("")


def test_ciphertext_from_another_key_cannot_be_read() -> None:
    # A value encrypted with a different key must be unreadable, otherwise a
    # stolen database dump could be opened with an unrelated key.
    other_key = Fernet("V19pwAKBBvM85X-KHh3zYNDq0l6LoROtAT8xGZ_2Jyg=")
    ciphertext = other_key.encrypt(b"written-with-the-other-key").decode("ascii")

    with pytest.raises(CredentialEncryptionError):
        decrypt_secret(ciphertext)
