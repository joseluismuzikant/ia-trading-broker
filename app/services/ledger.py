"""The paper ledger.

The first ledger is a copy of a portfolio snapshot: the same cash and the same
positions, priced with exact money. It is the paper account the strategy sizes
against. It is not the broker's record, and creating it never contacts IOL.

Day 5 is what updates the ledger from fills. Day 4 only creates it and reads it
back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.money import Money, money, quantity
from app.domain.portfolio import Portfolio
from app.domain.trading import Order, PaperLedger, PaperPosition
from app.domain.universe import UniverseConfig
from app.models import (
    BrokerConnection,
    ledger_snapshot_kind,
    paper_account_snapshot_kind,
    portfolio_snapshot_kind,
)
from app.services import connections as connections_service
from app.services import snapshots as snapshots_service


class LedgerError(Exception):
    """The paper ledger could not be created or safely updated."""


def ledger_from_portfolio(
    portfolio: Portfolio,
    *,
    source_snapshot_id: int | None = None,
    universe: UniverseConfig | None = None,
) -> PaperLedger:
    """Copy a portfolio snapshot into a paper ledger.

    A position without a price cannot be valued, so it is left out: sizing
    against an unknown value would be a guess. Cash defaults to zero only when
    the snapshot has none, and the ledger records that as zero rather than
    inventing a balance.

    ``universe`` supplies each instrument's quotation factor, so a bond quoted
    per 100 nominales is valued correctly. Without it every position is valued
    per 1, which is right for stocks and CEDEARs.
    """
    positions: list[PaperPosition] = []
    for position in portfolio.positions:
        price = position.last_price
        held = position.total_quantity
        if price is None or held is None or not position.instrument.symbol:
            continue
        symbol = position.instrument.symbol
        factor = universe.quotation_factor_for(symbol) if universe else 1
        positions.append(
            PaperPosition(
                symbol=symbol,
                quantity=quantity(held),
                average_price=money(position.average_price or price),
                last_price=money(price),
                market=position.instrument.market,
                currency=position.instrument.currency or portfolio.currency,
                quotation_factor=factor,
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
    universe: UniverseConfig | None = None,
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
    ledger = ledger_from_portfolio(portfolio, source_snapshot_id=source.id, universe=universe)
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

    factor = order.quotation_factor if order.quotation_factor > 0 else 1
    notional = money(filled_price.amount * filled / factor)
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
                    quotation_factor=factor,
                )
            )
        else:
            existing = positions[index]
            total_quantity = existing.quantity + filled
            new_average = money(
                (existing.average_price.amount * existing.quantity + filled_price.amount * filled)
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


# --- Paper accounts --------------------------------------------------------

#: The currency a paper account starts in. It matches the name IOL uses, so a
#: paper account and an IOL account compare cash the same way.
PAPER_DEFAULT_CURRENCY = "Peso_Argentino"


async def load_paper_account(
    db: AsyncSession, *, user_id: int, connection: BrokerConnection, country: str
) -> PaperLedger | None:
    """The saved state of a paper account, or None before it was started."""
    snapshot = await snapshots_service.latest_snapshot(
        db,
        user_id=user_id,
        connection_id=connection.id,
        kind=paper_account_snapshot_kind(country),
    )
    return PaperLedger.model_validate(snapshot.payload) if snapshot is not None else None


async def save_paper_account(
    db: AsyncSession, *, user_id: int, connection: BrokerConnection, ledger: PaperLedger
) -> PaperLedger:
    """Save the paper account as a new snapshot; the earlier states are kept."""
    await snapshots_service.save_snapshot(
        db,
        user_id=user_id,
        connection_id=connection.id,
        kind=paper_account_snapshot_kind(ledger.country),
        payload=ledger.model_dump(mode="json"),
    )
    return ledger


async def start_paper_account(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    source: BrokerConnection | None,
    country: str,
    starting: str,
    initial_cash: str | None,
    universe: UniverseConfig | None = None,
) -> PaperLedger:
    """Create (or reset) a paper account from its starting point.

    ``starting="cash"`` begins with only cash. ``starting="copy"`` copies the
    holdings and cash from the newest saved portfolio of the price-source
    connection, so a plan can be tried against a realistic account without
    touching it. Nothing here contacts a broker.
    """
    if starting == "copy":
        if source is None:
            raise LedgerError("A paper account needs a connection to copy from.")
        saved = await snapshots_service.latest_snapshot(
            db,
            user_id=user_id,
            connection_id=source.id,
            kind=portfolio_snapshot_kind(country),
        )
        if saved is None:
            raise LedgerError(
                "The account to copy has no saved portfolio for this country yet. "
                "Refresh its portfolio first."
            )
        ledger = ledger_from_portfolio(
            Portfolio.model_validate(saved.payload),
            source_snapshot_id=saved.id,
            universe=universe,
        )
    elif starting == "cash":
        try:
            cash = Decimal(str(initial_cash or "").strip().replace(",", ""))
        except InvalidOperation as exc:
            raise LedgerError("Starting cash must be a number.") from exc
        if cash <= 0:
            raise LedgerError("Starting cash must be above zero.")
        ledger = PaperLedger(
            country=country,
            currency=PAPER_DEFAULT_CURRENCY,
            cash=money(cash),
            positions=[],
            created_at=datetime.now(UTC),
        )
    else:
        raise LedgerError(f"Unknown starting point {starting!r}.")

    return await save_paper_account(db, user_id=user_id, connection=connection, ledger=ledger)


async def reset_paper_account(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    universe: UniverseConfig | None = None,
) -> PaperLedger:
    """Return a paper account to the starting point it was created with.

    It is a new saved state, not an edit: the earlier states stay in the
    snapshot history, and orders already recorded are left alone.
    """
    options = connections_service.load_options(connection)
    source = None
    if connection.price_source_connection_id is not None:
        source = await connections_service.get_connection(
            db, user_id=user_id, connection_id=connection.price_source_connection_id
        )
    return await start_paper_account(
        db,
        user_id=user_id,
        connection=connection,
        source=source,
        country=connection.country,
        starting=options.get("starting", "cash"),
        initial_cash=options.get("initial_cash"),
        universe=universe,
    )
