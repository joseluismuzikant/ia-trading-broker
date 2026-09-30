"""ORM models for the application foundation."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

#: Snapshot kinds. One saved copy is kept per connection and kind.
SNAPSHOT_PROFILE = "profile"
SNAPSHOT_ACCOUNT_STATUS = "account_status"
#: Country portfolios are stored per country. The kind carries the country so
#: switching country never overwrites another country's saved data.
SNAPSHOT_PORTFOLIO = "portfolio"


def portfolio_snapshot_kind(country: str) -> str:
    """Return the snapshot kind for one country portfolio."""
    return f"{SNAPSHOT_PORTFOLIO}:{country}"


def utcnow() -> datetime:
    """Return the current UTC time."""
    return datetime.now(timezone.utc)


class User(Base):
    """An application user."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, default=None)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<User id={self.id} username={self.username!r}>"


class Session(Base):
    """A server-side login session.

    Only the SHA-256 hash of the session token is stored. The raw token lives
    in the browser cookie and is never persisted.
    """

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)
    ip_address: Mapped[str | None] = mapped_column(String(64), default=None)

    user: Mapped[User] = relationship(back_populates="sessions")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Session id={self.id} user_id={self.user_id}>"


class BrokerConnection(Base):
    """A saved broker login for one user.

    The IOL password is stored only as Fernet ciphertext. It is decrypted in
    memory for the duration of a single broker call and is never written to a
    log, a template, or an error message.
    """

    __tablename__ = "broker_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "label", name="uq_broker_connections_user_label"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    broker: Mapped[str] = mapped_column(String(32), default="iol")
    label: Mapped[str] = mapped_column(String(80))
    country: Mapped[str] = mapped_column(String(32), default="argentina")
    username: Mapped[str] = mapped_column(String(120))
    password_encrypted: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    #: Short, safe text describing the last failure. Never a credential.
    last_error: Mapped[str | None] = mapped_column(String(200), default=None)
    last_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    user: Mapped[User] = relationship()
    snapshots: Mapped[list["Snapshot"]] = relationship(
        back_populates="connection", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<BrokerConnection id={self.id} user_id={self.user_id} "
            f"label={self.label!r} broker={self.broker!r}>"
        )


class Snapshot(Base):
    """A timestamped copy of one broker response.

    Rows are written on success and kept forever. When a later refresh fails,
    the newest row is marked stale instead of being deleted, so the last known
    good data stays on screen.
    """

    __tablename__ = "snapshots"
    __table_args__ = (
        Index("ix_snapshots_lookup", "connection_id", "kind", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("broker_connections.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    #: The validated broker response, stored as JSON.
    payload: Mapped[dict] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False)
    stale_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    error: Mapped[str | None] = mapped_column(String(200), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    connection: Mapped[BrokerConnection] = relationship(back_populates="snapshots")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<Snapshot id={self.id} kind={self.kind!r} "
            f"connection_id={self.connection_id} stale={self.is_stale}>"
        )
