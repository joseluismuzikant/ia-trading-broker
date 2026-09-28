"""Service-level tests for user and session rules."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Session
from app.security import hash_token
from app.services import auth as auth_service


async def test_create_user_hashes_the_password(db_session, password: str) -> None:
    user = await auth_service.create_user(db_session, username="alice", password=password)

    assert user.password_hash != password
    assert user.password_hash.startswith("$argon2id$")
    assert user.is_admin is False
    assert user.is_active is True


async def test_create_user_normalizes_username_and_email(db_session, password: str) -> None:
    user = await auth_service.create_user(
        db_session, username="  Alice  ", password=password, email="  Alice@Example.COM "
    )

    assert user.username == "alice"
    assert user.email == "alice@example.com"


async def test_duplicate_username_is_rejected(db_session, password: str) -> None:
    await auth_service.create_user(db_session, username="alice", password=password)

    with pytest.raises(auth_service.UserAlreadyExistsError):
        await auth_service.create_user(db_session, username="ALICE", password=password)


async def test_duplicate_email_is_rejected(db_session, password: str) -> None:
    await auth_service.create_user(
        db_session, username="alice", password=password, email="a@example.com"
    )

    with pytest.raises(auth_service.UserAlreadyExistsError):
        await auth_service.create_user(
            db_session, username="bob", password=password, email="a@example.com"
        )


async def test_empty_username_is_rejected(db_session, password: str) -> None:
    with pytest.raises(ValueError):
        await auth_service.create_user(db_session, username="   ", password=password)


async def test_authenticate_returns_the_user(db_session, password: str) -> None:
    await auth_service.create_user(db_session, username="alice", password=password)

    user = await auth_service.authenticate(
        db_session, username="Alice", password=password
    )

    assert user is not None
    assert user.username == "alice"


async def test_authenticate_rejects_wrong_password(db_session, password: str) -> None:
    await auth_service.create_user(db_session, username="alice", password=password)

    assert (
        await auth_service.authenticate(db_session, username="alice", password="nope")
        is None
    )


async def test_authenticate_rejects_inactive_users(db_session, password: str) -> None:
    await auth_service.create_user(
        db_session, username="alice", password=password, is_active=False
    )

    assert (
        await auth_service.authenticate(db_session, username="alice", password=password)
        is None
    )


async def test_session_token_is_stored_hashed(db_session, password: str) -> None:
    user = await auth_service.create_user(db_session, username="alice", password=password)

    raw_token, session = await auth_service.create_session(
        db_session, user=user, ttl_hours=1
    )

    assert session.token_hash == hash_token(raw_token)
    assert raw_token not in session.token_hash
    assert session.csrf_token != raw_token


async def test_created_session_resolves_to_its_user(db_session, password: str) -> None:
    user = await auth_service.create_user(db_session, username="alice", password=password)
    raw_token, _ = await auth_service.create_session(db_session, user=user, ttl_hours=1)

    found = await auth_service.get_session_by_token(db_session, raw_token)

    assert found is not None
    assert found.user_id == user.id


async def test_expired_session_is_removed(db_session, password: str) -> None:
    user = await auth_service.create_user(db_session, username="alice", password=password)
    raw_token, session = await auth_service.create_session(
        db_session, user=user, ttl_hours=-1
    )
    session_id = session.id

    assert await auth_service.get_session_by_token(db_session, raw_token) is None

    remaining = await db_session.scalar(select(Session).where(Session.id == session_id))
    assert remaining is None


async def test_unknown_token_returns_none(db_session) -> None:
    assert await auth_service.get_session_by_token(db_session, "unknown") is None
    assert await auth_service.get_session_by_token(db_session, "") is None


async def test_revoke_all_sessions(db_session, password: str) -> None:
    user = await auth_service.create_user(db_session, username="alice", password=password)
    await auth_service.create_session(db_session, user=user, ttl_hours=1)
    await auth_service.create_session(db_session, user=user, ttl_hours=1)

    removed = await auth_service.revoke_all_sessions(db_session, user.id)

    assert removed == 2
    assert await db_session.scalar(select(Session)) is None


async def test_delete_expired_sessions_keeps_valid_ones(db_session, password: str) -> None:
    user = await auth_service.create_user(db_session, username="alice", password=password)
    await auth_service.create_session(db_session, user=user, ttl_hours=1)
    await auth_service.create_session(db_session, user=user, ttl_hours=-1)

    removed = await auth_service.delete_expired_sessions(db_session)

    assert removed == 1
    assert len(list((await db_session.execute(select(Session))).scalars())) == 1


async def test_count_users(db_session, password: str) -> None:
    assert await auth_service.count_users(db_session) == 0

    await auth_service.create_user(db_session, username="alice", password=password)

    assert await auth_service.count_users(db_session) == 1
