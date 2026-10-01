"""ORM models for the application foundation and the paper-trading plan."""

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


def price_history_kind(symbol: str) -> str:
    """Return the snapshot kind for one instrument's saved price history."""
    return f"price_history:{symbol.strip().upper()}"


def ledger_snapshot_kind(country: str) -> str:
    """Return the snapshot kind for one country's paper ledger."""
    return f"paper_ledger:{country}"


#: A proposal is the complete trade plan. It is inserted once and never updated,
#: so the plan a user reviews cannot drift from the plan that was risk-checked.
PROPOSAL_PENDING = "pending_review"

#: Human approval lifecycle values. They mirror ``ApprovalStatus`` in the domain
#: layer but are repeated here so a query never imports the domain package.
APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_EXPIRED = "expired"
APPROVAL_CONSUMED = "consumed"

#: Order lifecycle values, mirroring ``OrderStatus``.
ORDER_PREPARED = "prepared"

#: Event categories used by the history page and the per-run timeline.
EVENT_RUN = "run"
EVENT_PROPOSAL = "proposal"
EVENT_APPROVAL = "approval"
EVENT_ORDER = "order"
EVENT_LEDGER = "ledger"


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


class ProposalRecord(Base):
    """A saved trade proposal.

    The row is inserted once. Nothing in the application updates it: a later
    analysis creates a new row, so an approval always points at exactly the plan
    that was reviewed. The ``payload`` is the full proposal, including its
    evidence, quantities, and risk results.
    """

    __tablename__ = "proposals"
    __table_args__ = (
        Index("ix_proposals_lookup", "connection_id", "country", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("broker_connections.id", ondelete="CASCADE"), index=True
    )
    country: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default=PROPOSAL_PENDING)
    approval_mode: Mapped[str] = mapped_column(String(32), default="HUMAN_IN_THE_LOOP")
    execution_mode: Mapped[str] = mapped_column(String(16), default="PAPER")
    #: The complete proposal: recommendations, evidence, quantities, and risk.
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    connection: Mapped[BrokerConnection] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<ProposalRecord id={self.id} connection_id={self.connection_id} "
            f"status={self.status!r}>"
        )


class Approval(Base):
    """A human decision on exactly one saved proposal.

    An approval is created when a proposal pauses for review, expires after
    :data:`~app.domain.trading.APPROVAL_TTL_HOURS`, and is consumed exactly once
    when the order service is reached. It is the single-use token that lets a
    paused run continue, and PostgreSQL, not an in-memory checkpoint, is what
    survives a restart.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        Index(
            "ix_approvals_lookup", "connection_id", "proposal_id", "requested_at"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("broker_connections.id", ondelete="CASCADE"), index=True
    )
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default=APPROVAL_PENDING)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    decided_by_user_id: Mapped[int | None] = mapped_column(Integer, default=None)
    #: Set when the approval is consumed by the order service. Single-use.
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    #: Short, safe text explaining a rejection or an expiry.
    reason: Mapped[str | None] = mapped_column(String(200), default=None)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<Approval id={self.id} proposal_id={self.proposal_id} "
            f"status={self.status!r}>"
        )


class OrderRecord(Base):
    """One saved order, from intent to result.

    The row is written *before* the executor is called, so a crash, a restart,
    or a repeated request finds the same order instead of creating a second one.
    The unique ``idempotency_key`` is what makes a resubmit safe. The ``payload``
    is the full :class:`~app.domain.trading.Order`.
    """

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_orders_idempotency_key"),
        Index("ix_orders_lookup", "connection_id", "proposal_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("broker_connections.id", ondelete="CASCADE"), index=True
    )
    proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("proposals.id", ondelete="SET NULL"), default=None, index=True
    )
    country: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(32), default=ORDER_PREPARED)
    execution_mode: Mapped[str] = mapped_column(String(16), default="PAPER")
    idempotency_key: Mapped[str] = mapped_column(String(160))
    broker_order_id: Mapped[str | None] = mapped_column(String(160), default=None)
    #: The full order, including quantity, prices, and the fill.
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<OrderRecord id={self.id} symbol={self.symbol!r} "
            f"status={self.status!r}>"
        )


class TradingEvent(Base):
    """One step in the local trading history.

    Events are append-only and are what the history page reads. They record what
    happened, in which mode, and against which proposal and order, so a run can
    be reconstructed without reading any logs.
    """

    __tablename__ = "trading_events"
    __table_args__ = (
        Index("ix_trading_events_lookup", "connection_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("broker_connections.id", ondelete="CASCADE"), index=True
    )
    proposal_id: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    order_id: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    category: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(48))
    message: Mapped[str] = mapped_column(String(300))
    #: The approval or execution mode this step ran under, when relevant.
    mode: Mapped[str | None] = mapped_column(String(32), default=None)
    payload: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<TradingEvent id={self.id} category={self.category!r} "
            f"type={self.event_type!r}>"
        )
