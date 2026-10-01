"""Local trading events: the append-only history.

Every meaningful step writes one row: an analysis was saved, an approval was
requested or decided, an order was prepared or filled, the ledger was updated.
The history page reads these rows, and a proposal's own timeline reads the rows
for one proposal. Nothing here calls the broker.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TradingEvent


async def record_event(
    db: AsyncSession,
    *,
    user_id: int,
    connection_id: int,
    category: str,
    event_type: str,
    message: str,
    proposal_id: int | None = None,
    order_id: int | None = None,
    mode: str | None = None,
    payload: dict | None = None,
) -> TradingEvent:
    """Append one event and return it."""
    event = TradingEvent(
        user_id=user_id,
        connection_id=connection_id,
        proposal_id=proposal_id,
        order_id=order_id,
        category=category,
        event_type=event_type,
        message=message[:300],
        mode=mode,
        payload=payload,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


async def list_events(
    db: AsyncSession,
    *,
    user_id: int,
    connection_id: int | None = None,
    proposal_id: int | None = None,
    limit: int = 100,
) -> list[TradingEvent]:
    """Return a user's events, newest first, optionally scoped further.

    The ``user_id`` filter is what keeps one user away from another user's
    history; a mismatch simply returns nothing.
    """
    statement = select(TradingEvent).where(TradingEvent.user_id == user_id)
    if connection_id is not None:
        statement = statement.where(TradingEvent.connection_id == connection_id)
    if proposal_id is not None:
        statement = statement.where(TradingEvent.proposal_id == proposal_id)
    statement = statement.order_by(
        TradingEvent.created_at.desc(), TradingEvent.id.desc()
    ).limit(limit)
    result = await db.execute(statement)
    return list(result.scalars())


async def list_events_for_proposal(
    db: AsyncSession, *, user_id: int, proposal_id: int
) -> list[TradingEvent]:
    """Return one proposal's events, oldest first, as a timeline."""
    result = await db.execute(
        select(TradingEvent)
        .where(
            TradingEvent.user_id == user_id,
            TradingEvent.proposal_id == proposal_id,
        )
        .order_by(TradingEvent.created_at, TradingEvent.id)
    )
    return list(result.scalars())


async def count_events(db: AsyncSession, *, user_id: int) -> int:
    """Return how many events the user has."""
    result = await db.execute(
        select(TradingEvent.id).where(TradingEvent.user_id == user_id)
    )
    return len(list(result.scalars()))
