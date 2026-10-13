"""Saved orders and their single-use idempotency keys.

The order row is written *before* the executor is called. If the process stops,
the browser is refreshed, or the approval is replayed, the same key is found and
no second order is created. This is what makes paper execution safe to retry.

The unique ``idempotency_key`` is ``proposal:{id}:{side}:{symbol}``: one order
per side and instrument for one immutable plan.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.trading import Order
from app.models import OrderRecord


def idempotency_key(proposal_id: int, symbol: str, side: str, attempt: int = 1) -> str:
    """Return the one key that identifies an order for one plan.

    The first attempt is ``proposal:{id}:{side}:{symbol}``. A retry after a
    proven failure appends ``:retryN``, so the same trade can be tried again
    without ever colliding with, or duplicating, the earlier attempt.
    """
    base = f"proposal:{proposal_id}:{side}:{symbol.strip().upper()}"
    return base if attempt <= 1 else f"{base}:retry{attempt}"


#: States where the broker may still act, or where the outcome is unknown.
#: A second order for the same trade is never allowed while one of these stands.
UNCONFIRMED_STATES = {"prepared", "submitted", "accepted", "unknown", "partially_filled"}
#: States that prove the order did not happen. Only these allow a retry.
FAILED_STATES = {"rejected", "cancelled"}


async def next_attempt(
    db: AsyncSession, *, user_id: int, proposal_id: int, symbol: str, side: str
) -> tuple[int, bool, str]:
    """The attempt number for one trade, and whether it may be sent again.

    Returns ``(attempt, allowed, reason)``. A trade with no order yet is
    attempt 1. A proven failure (rejected or cancelled) allows a retry. A
    filled trade is done, and anything the broker may still act on — including
    an unknown outcome — blocks further attempts until it is reconciled.
    """
    wanted = symbol.strip().upper()
    rows = [
        row
        for row in await list_orders_for_proposal(db, user_id=user_id, proposal_id=proposal_id)
        if row.symbol == wanted and row.side == side.strip().upper()
    ]
    if not rows:
        return 1, True, ""
    latest = rows[-1]
    if latest.status in FAILED_STATES:
        return len(rows) + 1, True, ""
    if latest.status == "filled":
        return len(rows), False, "this trade is already filled"
    return (
        len(rows),
        False,
        "an order for this trade is already at the broker and not settled",
    )


def order_from_record(record: OrderRecord) -> Order:
    """Read the full order out of a saved row."""
    return Order.model_validate(record.payload)


async def get_order(db: AsyncSession, *, user_id: int, order_id: int) -> OrderRecord | None:
    """Return one order owned by the user, or None."""
    result = await db.execute(
        select(OrderRecord).where(
            OrderRecord.id == order_id,
            OrderRecord.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def get_by_key(db: AsyncSession, *, user_id: int, key: str) -> OrderRecord | None:
    """Return the order with this idempotency key for the user, or None."""
    result = await db.execute(
        select(OrderRecord).where(
            OrderRecord.user_id == user_id,
            OrderRecord.idempotency_key == key,
        )
    )
    return result.scalar_one_or_none()


async def prepare_order(
    db: AsyncSession,
    *,
    user_id: int,
    connection_id: int,
    proposal_id: int,
    country: str,
    order: Order,
    attempt: int = 1,
) -> tuple[OrderRecord, bool]:
    """Save a prepared order, or return the existing one.

    Returns ``(record, created)``. ``created`` is False when the key already
    existed, which is the signal that the order must not be sent again.
    """
    key = idempotency_key(proposal_id, order.symbol, order.side, attempt)
    existing = await get_by_key(db, user_id=user_id, key=key)
    if existing is not None:
        return existing, False

    record = OrderRecord(
        user_id=user_id,
        connection_id=connection_id,
        proposal_id=proposal_id,
        country=country,
        symbol=order.symbol,
        side=order.side,
        status=order.status,
        execution_mode=order.execution_mode,
        idempotency_key=key,
        broker_order_id=order.broker_order_id,
        payload=order.model_dump(mode="json"),
        created_at=order.created_at or datetime.now(UTC),
    )
    db.add(record)
    try:
        await db.commit()
    except IntegrityError:
        # Two requests raced for the same key. The database kept one row; the
        # loser must treat the order as already existing and never send it.
        await db.rollback()
        winner = await get_by_key(db, user_id=user_id, key=key)
        if winner is None:
            raise
        return winner, False
    await db.refresh(record)
    return record, True


async def update_order(db: AsyncSession, record: OrderRecord, order: Order) -> OrderRecord:
    """Write an order's latest state back to its row."""
    record.status = order.status
    record.broker_order_id = order.broker_order_id
    record.payload = order.model_dump(mode="json")
    record.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(record)
    return record


async def list_orders_for_proposal(
    db: AsyncSession, *, user_id: int, proposal_id: int
) -> list[OrderRecord]:
    """Return every order for one proposal, oldest first."""
    result = await db.execute(
        select(OrderRecord)
        .where(
            OrderRecord.user_id == user_id,
            OrderRecord.proposal_id == proposal_id,
        )
        .order_by(OrderRecord.created_at, OrderRecord.id)
    )
    return list(result.scalars())


async def delete_orders_for_proposal(db: AsyncSession, *, user_id: int, proposal_id: int) -> int:
    """Delete every order of one proposal. Returns how many rows went."""
    result = await db.execute(
        delete(OrderRecord).where(
            OrderRecord.user_id == user_id,
            OrderRecord.proposal_id == proposal_id,
        )
    )
    await db.commit()
    return int(result.rowcount or 0)


async def list_orders(
    db: AsyncSession,
    *,
    user_id: int,
    connection_id: int | None = None,
    limit: int = 200,
) -> list[OrderRecord]:
    """Return a user's orders, newest first, optionally for one connection."""
    statement = select(OrderRecord).where(OrderRecord.user_id == user_id)
    if connection_id is not None:
        statement = statement.where(OrderRecord.connection_id == connection_id)
    statement = statement.order_by(OrderRecord.created_at.desc(), OrderRecord.id.desc()).limit(
        limit
    )
    result = await db.execute(statement)
    return list(result.scalars())


async def count_orders(db: AsyncSession, *, user_id: int) -> int:
    """Return how many orders the user has."""
    result = await db.execute(select(OrderRecord.id).where(OrderRecord.user_id == user_id))
    return len(list(result.scalars()))
