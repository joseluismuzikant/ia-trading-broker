"""Human approvals for one saved proposal.

An approval is the single-use, expiring token that lets a paused run continue.
It is created when a proposal is first shown, expires after
:data:`~app.domain.trading.APPROVAL_TTL_HOURS`, and is consumed exactly once when
the order service is reached. PostgreSQL is the source of truth, so a restart
between the request and the decision changes nothing.

Only the human decision lives here. Running the workflow, creating the order,
and updating the ledger belong to the execution service.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.trading import APPROVAL_TTL_HOURS
from app.models import (
    APPROVAL_APPROVED,
    APPROVAL_CONSUMED,
    APPROVAL_EXPIRED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    Approval,
)

logger = logging.getLogger("ia_trading_broker.approvals")


class ApprovalError(Exception):
    """Base class for approval service failures."""


class ApprovalNotFoundError(ApprovalError):
    """No approval matched the request for this user."""


class ApprovalNotPendingError(ApprovalError):
    """The approval was already approved, rejected, expired, or consumed."""


class ApprovalExpiredError(ApprovalError):
    """The approval expired before it was used."""


def _aware(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime.

    SQLite stores timestamps without an offset, so a value read back can be
    naive. Treating it as UTC keeps comparisons against ``datetime.now(UTC)``
    valid in both the real database and the test database.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def is_expired(approval: Approval, *, now: datetime | None = None) -> bool:
    """True when a pending approval is past its expiry."""
    current = now or datetime.now(UTC)
    return (
        approval.status == APPROVAL_PENDING
        and _aware(approval.expires_at) <= current
    )


async def request_approval(
    db: AsyncSession,
    *,
    user_id: int,
    connection_id: int,
    proposal_id: int,
    ttl_hours: int = APPROVAL_TTL_HOURS,
) -> Approval:
    """Create the pending approval for a proposal, or return the open one.

    A proposal that already has a pending or approved approval is not given a
    second one, so re-running the workflow never creates duplicate requests.
    """
    existing = await latest_approval(db, user_id=user_id, proposal_id=proposal_id)
    if existing is not None and existing.status in {APPROVAL_PENDING, APPROVAL_APPROVED}:
        return existing

    now = datetime.now(UTC)
    approval = Approval(
        user_id=user_id,
        connection_id=connection_id,
        proposal_id=proposal_id,
        status=APPROVAL_PENDING,
        requested_at=now,
        expires_at=now + timedelta(hours=ttl_hours),
    )
    db.add(approval)
    await db.commit()
    await db.refresh(approval)
    logger.info(
        "approval id=%s requested for proposal id=%s", approval.id, proposal_id
    )
    return approval


async def get_approval(
    db: AsyncSession, *, user_id: int, approval_id: int
) -> Approval | None:
    """Return one approval owned by the user, or None."""
    result = await db.execute(
        select(Approval).where(
            Approval.id == approval_id,
            Approval.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def latest_approval(
    db: AsyncSession, *, user_id: int, proposal_id: int
) -> Approval | None:
    """Return the newest approval for a proposal, or None."""
    result = await db.execute(
        select(Approval)
        .where(
            Approval.user_id == user_id,
            Approval.proposal_id == proposal_id,
        )
        .order_by(Approval.requested_at.desc(), Approval.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def refresh_status(
    db: AsyncSession, approval: Approval, *, now: datetime | None = None
) -> Approval:
    """Expire a pending approval whose window has passed."""
    if is_expired(approval, now=now):
        approval.status = APPROVAL_EXPIRED
        approval.reason = approval.reason or "The approval window expired."
        await db.commit()
        await db.refresh(approval)
        logger.info("approval id=%s expired", approval.id)
    return approval


async def approve(
    db: AsyncSession, *, user_id: int, proposal_id: int, now: datetime | None = None
) -> Approval:
    """Approve the open approval for a proposal, once.

    Raises when there is no approval, when it is already decided, or when it has
    expired. An already-approved approval is refused too, so a repeated request
    cannot slip through to a second execution.
    """
    approval = await latest_approval(db, user_id=user_id, proposal_id=proposal_id)
    if approval is None:
        raise ApprovalNotFoundError("No approval is open for this proposal.")
    await refresh_status(db, approval, now=now)

    if approval.status == APPROVAL_APPROVED:
        raise ApprovalNotPendingError("This proposal is already approved.")
    if approval.status != APPROVAL_PENDING:
        raise ApprovalNotPendingError(
            f"This approval is {approval.status}; it can no longer be approved."
        )

    approval.status = APPROVAL_APPROVED
    approval.decided_at = now or datetime.now(UTC)
    approval.decided_by_user_id = user_id
    await db.commit()
    await db.refresh(approval)
    logger.info("approval id=%s approved", approval.id)
    return approval


async def reject(
    db: AsyncSession,
    *,
    user_id: int,
    proposal_id: int,
    reason: str | None = None,
    now: datetime | None = None,
) -> Approval:
    """Reject the open approval for a proposal, once."""
    approval = await latest_approval(db, user_id=user_id, proposal_id=proposal_id)
    if approval is None:
        raise ApprovalNotFoundError("No approval is open for this proposal.")
    await refresh_status(db, approval, now=now)

    if approval.status != APPROVAL_PENDING:
        raise ApprovalNotPendingError(
            f"This approval is {approval.status}; it can no longer be rejected."
        )

    approval.status = APPROVAL_REJECTED
    approval.decided_at = now or datetime.now(UTC)
    approval.decided_by_user_id = user_id
    approval.reason = (reason or "Rejected by the user.")[:200]
    await db.commit()
    await db.refresh(approval)
    logger.info("approval id=%s rejected", approval.id)
    return approval


async def consume(
    db: AsyncSession, approval: Approval, *, now: datetime | None = None
) -> Approval:
    """Mark an approved approval as used. It can never be used again."""
    if approval.status != APPROVAL_APPROVED:
        raise ApprovalNotPendingError(
            f"Only an approved approval can be consumed, not {approval.status}."
        )
    approval.status = APPROVAL_CONSUMED
    approval.used_at = now or datetime.now(UTC)
    await db.commit()
    await db.refresh(approval)
    logger.info("approval id=%s consumed", approval.id)
    return approval
