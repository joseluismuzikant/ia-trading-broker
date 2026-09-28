"""User and session persistence logic.

Keeping this logic in one place means routes stay thin and tests can cover the
rules without going through HTTP.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Session, User
from app.security import (
    generate_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    verify_password,
)


class UserAlreadyExistsError(Exception):
    """Raised when creating a user with a taken username or email."""


def normalize_username(username: str) -> str:
    """Normalize a username for comparisons and storage."""
    return username.strip().lower()


def normalize_email(email: str | None) -> str | None:
    """Normalize an optional email address."""
    if email is None:
        return None
    cleaned = email.strip().lower()
    return cleaned or None


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Attach UTC to naive datetimes read back from some databases."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def create_user(
    db: AsyncSession,
    *,
    username: str,
    password: str,
    email: str | None = None,
    is_admin: bool = False,
    is_active: bool = True,
) -> User:
    """Create a user, hashing the password with Argon2id."""
    normalized_username = normalize_username(username)
    if not normalized_username:
        raise ValueError("username must not be empty")
    if not password:
        raise ValueError("password must not be empty")

    normalized_email = normalize_email(email)

    existing = await db.scalar(select(User).where(User.username == normalized_username))
    if existing is not None:
        raise UserAlreadyExistsError(f"username {normalized_username!r} already exists")

    if normalized_email is not None:
        existing_email = await db.scalar(
            select(User).where(User.email == normalized_email)
        )
        if existing_email is not None:
            raise UserAlreadyExistsError(f"email {normalized_email!r} already exists")

    user = User(
        username=normalized_username,
        email=normalized_email,
        password_hash=hash_password(password),
        is_admin=is_admin,
        is_active=is_active,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    """Return a user by username or None."""
    return await db.scalar(
        select(User).where(User.username == normalize_username(username))
    )


async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    """Return a user by primary key or None."""
    return await db.get(User, user_id)


async def count_users(db: AsyncSession) -> int:
    """Return the number of users in the database."""
    return await db.scalar(select(func.count()).select_from(User)) or 0


async def authenticate(db: AsyncSession, *, username: str, password: str) -> User | None:
    """Return the user when the credentials are valid, otherwise None.

    The same code path runs for unknown users and wrong passwords so callers
    cannot tell the two apart.
    """
    user = await get_user_by_username(db, username)
    if user is None:
        # Still verify against a dummy hash to keep timing similar.
        verify_password(password, hash_password("dummy-password-for-timing"))
        return None

    if not verify_password(password, user.password_hash):
        return None

    if not user.is_active:
        return None

    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        await db.commit()

    return user


async def create_session(
    db: AsyncSession,
    *,
    user: User,
    ttl_hours: int,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, Session]:
    """Create a server-side session and return ``(raw_token, session)``."""
    raw_token = generate_token()
    session = Session(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        csrf_token=generate_token(),
        expires_at=utc_now() + timedelta(hours=ttl_hours),
        user_agent=(user_agent or "")[:255] or None,
        ip_address=(ip_address or "")[:64] or None,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return raw_token, session


async def get_session_by_token(db: AsyncSession, raw_token: str) -> Session | None:
    """Return a non-expired session for the raw cookie token, or None."""
    if not raw_token:
        return None

    session = await db.scalar(
        select(Session).where(Session.token_hash == hash_token(raw_token))
    )
    if session is None:
        return None

    if ensure_utc(session.expires_at) <= utc_now():
        await revoke_session(db, session)
        return None

    return session


async def touch_session(db: AsyncSession, session: Session) -> None:
    """Record that the session was used."""
    session.last_seen_at = utc_now()
    await db.commit()


async def revoke_session(db: AsyncSession, session: Session) -> None:
    """Delete a single session."""
    await db.delete(session)
    await db.commit()


async def revoke_all_sessions(db: AsyncSession, user_id: int) -> int:
    """Delete every session for a user and return how many were removed."""
    result = await db.execute(delete(Session).where(Session.user_id == user_id))
    await db.commit()
    return result.rowcount or 0


async def delete_expired_sessions(db: AsyncSession) -> int:
    """Delete sessions that are already expired."""
    result = await db.execute(delete(Session).where(Session.expires_at <= utc_now()))
    await db.commit()
    return result.rowcount or 0
