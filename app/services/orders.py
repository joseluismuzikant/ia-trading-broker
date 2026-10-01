"""Saved orders and their single-use idempotency keys.

The order row is written *before* the executor is called. If the process stops,
the browser is refreshed, or the approval is replayed, the same key is found and
no second order is created. This is what makes paper execution safe to retry.

The unique ``idempotency_key`` is ``proposal:{id}:{side}:{symbol}``: one order
per side and instrument for one immutable plan.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.trading import Order
from app.models import OrderRecord


def idempotency_key(proposal_id: int, symbol: str, side: str) -> str:
    """Return the one key that identifies an order for one plan."""
    return f"proposal:{proposal_id}:{side}:{symbol.strip().upper()}"


def order_from_record(record: OrderRecord) -> Order:
    """Read the full order out of a saved row."""
    return Order.model_validate(record.payload)


async def get_order(
    db: AsyncSession, *, user_id: int, order_id: int
) -> OrderRecord | None:
    """Return one order owned by the user, or None."""
    result = await db.execute(
        select(OrderRecord).where(
            OrderRecord.id == order_id,
            OrderRecord.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def get_by_key(
    db: AsyncSession, *, user_id: int, key: str
) -> OrderRecord | None:
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
) -> tuple[OrderRecord, bool]:
    """Save a prepared order, or return the existing one.

    Returns ``(record, created)``. ``created`` is False when the key already
    existed, which is the signal that the order must not be sent again.
    """
    key = idempotency_key(proposal_id, order.symbol, order.side)
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
    await db.commit()
    await db.refresh(record)
    return record, True


async def update_order(
    db: AsyncSession, record: OrderRecord, order: Order
) -> OrderRecord:
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
    statement = statement.order_by(
        OrderRecord.created_at.desc(), OrderRecord.id.desc()
    ).limit(limit)
    result = await db.execute(statement)
    return list(result.scalars())


async def count_orders(db: AsyncSession, *, user_id: int) -> int:
    """Return how many orders the user has."""
    result = await db.execute(
        select(OrderRecord.id).where(OrderRecord.user_id == user_id)
    )
    return len(list(result.scalars()))
