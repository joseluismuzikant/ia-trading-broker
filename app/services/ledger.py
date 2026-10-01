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

from app.domain.money import Money, money, quantity
from app.domain.portfolio import Portfolio
from app.domain.trading import Order, PaperLedger, PaperPosition
from app.models import BrokerConnection, ledger_snapshot_kind, portfolio_snapshot_kind
from app.services import snapshots as snapshots_service


class LedgerError(Exception):
    """The paper ledger could not be created or safely updated."""


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


async def save_ledger(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    ledger: PaperLedger,
) -> PaperLedger:
    """Save the current paper ledger as the newest snapshot.

    A fill writes a new snapshot rather than editing the old one, so the ledger
    before a fill is never lost.
    """
    await snapshots_service.save_snapshot(
        db,
        user_id=user_id,
        connection_id=connection.id,
        kind=ledger_snapshot_kind(ledger.country),
        payload=ledger.model_dump(mode="json"),
    )
    return ledger


def apply_fill(
    ledger: PaperLedger,
    order: Order,
    *,
    filled_quantity,
    filled_price: Money,
) -> PaperLedger:
    """Return the ledger after one fill.

    A buy adds to cash spent and to the position, weighted into the average
    price. A sell releases cash and reduces the position, which is removed when
    it reaches zero. The ledger is frozen, so this returns a new one and leaves
    the caller's copy untouched.
    """
    filled = quantity(filled_quantity)
    if filled <= 0:
        raise LedgerError("A fill quantity must be positive.")
    if filled > order.quantity:
        raise LedgerError("A fill cannot exceed the order quantity.")
    if not filled_price.is_positive:
        raise LedgerError("A fill price must be positive.")

    notional = Money(filled_price.amount * filled)
    positions = list(ledger.positions)
    index = next(
        (i for i, position in enumerate(positions) if position.symbol == order.symbol),
        None,
    )

    if order.side == "BUY":
        if notional > ledger.cash:
            raise LedgerError("A paper buy cannot spend more cash than is available.")
        if index is None:
            positions.append(
                PaperPosition(
                    symbol=order.symbol,
                    quantity=filled,
                    average_price=filled_price,
                    last_price=filled_price,
                    currency=ledger.currency,
                )
            )
        else:
            existing = positions[index]
            total_quantity = existing.quantity + filled
            new_average = money(
                (
                    existing.average_price.amount * existing.quantity
                    + filled_price.amount * filled
                )
                / total_quantity
            )
            positions[index] = existing.model_copy(
                update={
                    "quantity": total_quantity,
                    "average_price": new_average,
                    "last_price": filled_price,
                }
            )
        cash = ledger.cash - notional
    else:
        existing = positions[index] if index is not None else None
        if existing is None:
            raise LedgerError(f"Cannot sell {order.symbol}; no paper position exists.")
        if filled > existing.quantity:
            raise LedgerError(
                f"Cannot sell {filled} {order.symbol}; only {existing.quantity} is held."
            )
        remaining = existing.quantity - filled
        if remaining == 0:
            positions.pop(index)
        else:
            positions[index] = existing.model_copy(
                update={"quantity": remaining, "last_price": filled_price}
            )
        cash = ledger.cash + notional

    return PaperLedger(
        country=ledger.country,
        currency=ledger.currency,
        cash=cash,
        positions=positions,
        source_snapshot_id=ledger.source_snapshot_id,
        created_at=datetime.now(UTC),
    )
