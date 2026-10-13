"""Shared helpers for the Day 5 tests.

They build the smallest account that produces a real plan: a flat paper ledger
with cash and a saved, gently rising price history. That is enough for the
strategy to propose buys and the workflow to reach the approval step.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.domain.money import money, quantity
from app.domain.trading import PaperLedger, Proposal, Recommendation
from app.infrastructure.iol.schemas import Quote
from app.models import price_history_kind
from app.services import broker as broker_service
from app.services import connections as connections_service
from app.services import snapshots as snapshots_service

DEFAULT_CASH = "100000.00"


def uptrend_quotes(count: int = 45, start: float = 100.0) -> list[Quote]:
    """A rising series with small pullbacks, the same shape the fake serves."""
    quotes = []
    day = date(2024, 1, 1)
    for index in range(count):
        close = start + index * 0.6 + (1.5 if index % 2 == 0 else -1.0)
        quotes.append(
            Quote.model_validate(
                {
                    "ultimoPrecio": round(close, 2),
                    "maximo": round(close + 1.0, 2),
                    "minimo": round(close - 1.0, 2),
                    "volumenNominal": 100_000 + index * 500,
                    "fechaHora": (day + timedelta(days=index)).isoformat() + "T15:00:00",
                }
            )
        )
    return quotes


def flat_ledger(cash: str = DEFAULT_CASH) -> PaperLedger:
    """A paper account with cash and no positions."""
    return PaperLedger(
        country="argentina",
        currency="Peso_Argentino",
        cash=money(cash),
        positions=[],
    )


def buy_proposal(symbol: str = "GGAL", units: str = "100") -> Proposal:
    """A one-order plan: the smallest proposal that can reach execution.

    Execution tests need a saved plan with a BUY; how the rebalance sized it is
    covered by ``test_rebalance``, so the order is written down directly.
    """
    from app.services import analysis as analysis_service

    evidence = analysis_service.evidence_for(uptrend_quotes())
    price = evidence.last_price
    assert price is not None
    notional = money(price.amount * Decimal(units))
    order = Recommendation(
        symbol=symbol,
        name=f"{symbol} S.A.",
        action="BUY",
        quantity=quantity(units),
        limit_price=price,
        notional=notional,
        market="BCBA",
        rationale="A test order.",
        evidence=evidence,
    )
    ledger = flat_ledger()
    return Proposal(
        country=ledger.country,
        currency=ledger.currency,
        portfolio_value=ledger.total_value,
        cash=ledger.cash,
        recommendations=[order],
        turnover=float(notional / ledger.total_value),
        summary=f"Planned: BUY {units} {symbol}.",
    )


async def make_connection(db, user, fake):
    """Create a stored IOL connection for the fake broker."""
    return await connections_service.create_connection(
        db,
        user_id=user.id,
        label="My IOL account",
        username=fake.username,
        password=fake.password,
    )


async def save_flat_ledger(db, *, user_id: int, connection, cash: str = DEFAULT_CASH):
    """Save a flat paper ledger, replacing any earlier one."""
    from app.services import ledger as ledger_service

    return await ledger_service.save_ledger(
        db,
        user_id=user_id,
        connection=connection,
        ledger=flat_ledger(cash),
    )


async def save_price_history(db, *, user_id: int, connection, symbols=("GGAL", "YPFD")) -> None:
    """Read and save price history through the fake broker."""
    for symbol in symbols:
        await broker_service.read_price_history(
            db,
            user_id=user_id,
            connection=connection,
            market="BCBA",
            symbol=symbol,
        )


async def save_price_history_snapshot(
    db, *, user_id: int, connection, symbol: str, quotes: list[Quote]
) -> None:
    """Save price history directly, bypassing the broker.

    Used to simulate a price move between the review and the approval.
    """
    await snapshots_service.save_snapshot(
        db,
        user_id=user_id,
        connection_id=connection.id,
        kind=price_history_kind(symbol),
        payload={
            "symbol": symbol,
            "market": "BCBA",
            "quotes": [quote.model_dump(mode="json", by_alias=True) for quote in quotes],
        },
    )


async def prepare_tradeable_account(db, *, user, connection, fake=None) -> None:
    """A flat ledger plus saved price history, so a run proposes buys."""
    await save_flat_ledger(db, user_id=user.id, connection=connection)
    await save_price_history(db, user_id=user.id, connection=connection)
