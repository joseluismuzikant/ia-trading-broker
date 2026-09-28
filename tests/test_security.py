"""Tests for password hashing and token helpers."""

from __future__ import annotations

from app.security import (
    generate_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    tokens_equal,
    verify_password,
)


def test_hash_password_uses_argon2id() -> None:
    hashed = hash_password("s3cret-password")
    assert hashed.startswith("$argon2id$")
    assert "s3cret-password" not in hashed


def test_hash_password_is_salted() -> None:
    assert hash_password("same-password") != hash_password("same-password")


def test_verify_password_accepts_correct_password() -> None:
    hashed = hash_password("s3cret-password")
    assert verify_password("s3cret-password", hashed) is True


def test_verify_password_rejects_wrong_password() -> None:
    hashed = hash_password("s3cret-password")
    assert verify_password("wrong-password", hashed) is False


def test_verify_password_rejects_garbage_hash() -> None:
    assert verify_password("anything", "not-a-hash") is False


def test_fresh_hash_does_not_need_rehash() -> None:
    assert password_needs_rehash(hash_password("s3cret-password")) is False


def test_invalid_hash_needs_rehash() -> None:
    assert password_needs_rehash("not-a-hash") is True


def test_hash_token_is_stable_and_hides_the_token() -> None:
    token = generate_token()
    digest = hash_token(token)
    assert digest == hash_token(token)
    assert len(digest) == 64
    assert token not in digest


def test_hash_token_rejects_empty() -> None:
    import pytest

    with pytest.raises(ValueError):
        hash_token("")


def test_generate_token_is_random() -> None:
    assert generate_token() != generate_token()


def test_tokens_equal() -> None:
    token = generate_token()
    assert tokens_equal(token, token) is True
    assert tokens_equal(token, generate_token()) is False
    assert tokens_equal("", "") is False
