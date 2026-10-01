"""The paper ledger.

The first ledger is a copy of a portfolio snapshot: the same cash and the same
positions, priced with exact money. It is the paper account the strategy sizes
against. It is not the broker's record, and creating it never contacts IOL.

Day 5 is what updates the ledger from fills. Day 4 only creates it and reads it
back.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.money import money, quantity
from app.domain.portfolio import Portfolio
from app.domain.trading import PaperLedger, PaperPosition
from app.models import BrokerConnection, ledger_snapshot_kind, portfolio_snapshot_kind
from app.services import snapshots as snapshots_service


class LedgerError(Exception):
    """The paper ledger could not be created."""


def ledger_from_portfolio(
    portfolio: Portfolio, *, source_snapshot_id: int | None = None
) -> PaperLedger:
    """Copy a portfolio snapshot into a paper ledger.

    A position without a price cannot be valued, so it is left out: sizing
    against an unknown value would be a guess. Cash defaults to zero only when
    the snapshot has none, and the ledger records that as zero rather than
    inventing a balance.
    """
    positions: list[PaperPosition] = []
    for position in portfolio.positions:
        price = position.last_price
        held = position.total_quantity
        if price is None or held is None or not position.instrument.symbol:
            continue
        positions.append(
            PaperPosition(
                symbol=position.instrument.symbol,
                quantity=quantity(held),
                average_price=money(position.average_price or price),
                last_price=money(price),
                market=position.instrument.market,
                currency=position.instrument.currency or portfolio.currency,
            )
        )

    return PaperLedger(
        country=portfolio.country,
        currency=portfolio.currency,
        cash=money(portfolio.cash or 0),
        positions=positions,
        source_snapshot_id=source_snapshot_id,
        created_at=datetime.now(UTC),
    )


async def create_ledger(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    country: str,
) -> PaperLedger:
    """Create the paper ledger from the newest portfolio snapshot.

    Raises :class:`LedgerError` when that snapshot does not exist yet, because a
    ledger must start from real positions rather than an empty guess.
    """
    source = await snapshots_service.latest_snapshot(
        db,
        user_id=user_id,
        connection_id=connection.id,
        kind=portfolio_snapshot_kind(country),
    )
    if source is None:
        raise LedgerError(
            "No portfolio snapshot is saved for this country yet. Refresh the "
            "portfolio before starting a paper ledger."
        )

    portfolio = Portfolio.model_validate(source.payload)
    ledger = ledger_from_portfolio(portfolio, source_snapshot_id=source.id)
    await snapshots_service.save_snapshot(
        db,
        user_id=user_id,
        connection_id=connection.id,
        kind=ledger_snapshot_kind(country),
        payload=ledger.model_dump(mode="json"),
    )
    return ledger


async def load_ledger(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    country: str,
) -> PaperLedger | None:
    """Return the newest paper ledger, or None when none has been created."""
    snapshot = await snapshots_service.latest_snapshot(
        db,
        user_id=user_id,
        connection_id=connection.id,
        kind=ledger_snapshot_kind(country),
    )
    if snapshot is None:
        return None
    return PaperLedger.model_validate(snapshot.payload)
