"""Snapshot rules: keep the last known good broker data, mark it stale.

A snapshot is written only on a successful broker read. A failed refresh never
deletes anything: it flags the newest row as stale and records why.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Snapshot


async def save_snapshot(
    db: AsyncSession,
    *,
    user_id: int,
    connection_id: int,
    kind: str,
    payload: dict,
) -> Snapshot:
    """Store a fresh, current snapshot."""
    snapshot = Snapshot(
        user_id=user_id,
        connection_id=connection_id,
        kind=kind,
        payload=payload,
        fetched_at=datetime.now(UTC),
        is_stale=False,
        stale_since=None,
        error=None,
    )
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot


async def latest_snapshot(
    db: AsyncSession,
    *,
    user_id: int,
    connection_id: int,
    kind: str,
) -> Snapshot | None:
    """Return the newest snapshot for one connection and kind, if any."""
    result = await db.execute(
        select(Snapshot)
        .where(
            Snapshot.user_id == user_id,
            Snapshot.connection_id == connection_id,
            Snapshot.kind == kind,
        )
        .order_by(Snapshot.fetched_at.desc(), Snapshot.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def mark_latest_stale(
    db: AsyncSession,
    *,
    user_id: int,
    connection_id: int,
    kind: str,
    message: str,
) -> Snapshot | None:
    """Flag the newest snapshot as stale after a failed refresh.

    Returns the snapshot that was marked, or None when nothing was ever saved.
    """
    snapshot = await latest_snapshot(
        db, user_id=user_id, connection_id=connection_id, kind=kind
    )
    if snapshot is None:
        return None

    now = datetime.now(UTC)
    snapshot.is_stale = True
    snapshot.stale_since = snapshot.stale_since or now
    snapshot.error = message[:200]
    await db.commit()
    await db.refresh(snapshot)
    return snapshot


async def count_snapshots(
    db: AsyncSession, *, user_id: int, connection_id: int
) -> int:
    """Return how many snapshots are stored for a connection."""
    result = await db.execute(
        select(Snapshot.id).where(
            Snapshot.user_id == user_id,
            Snapshot.connection_id == connection_id,
        )
    )
    return len(list(result.scalars()))
