"""Tests for the paper ledger."""

from __future__ import annotations

import pytest

from app.domain.money import money
from app.domain.portfolio import Instrument, Portfolio, Position
from app.domain.trading import PaperLedger
from app.services import broker as broker_service
from app.services import connections as connections_service
from app.services import ledger as ledger_service


def instrument(symbol="GGAL", currency="Peso_Argentino") -> Instrument:
    return Instrument(
        symbol=symbol,
        description="Grupo Financiero Galicia",
        market="BCBA",
        instrument_type="ACCIONES",
        currency=currency,
    )


def portfolio(cash="100.00", positions=None) -> Portfolio:
    return Portfolio(
        country="argentina",
        currency="Peso_Argentino",
        cash=float(cash),
        positions=positions or [],
    )


# --- Building a ledger from a portfolio -----------------------------------


def test_a_ledger_copies_cash_and_priced_positions() -> None:
    source = portfolio(
        cash="500.00",
        positions=[
            Position(
                instrument=instrument("GGAL"),
                total_quantity=100,
                available_quantity=75,
                committed_quantity=25,
                last_price=60.555,
                average_price=50.0,
                market_value=6055.5,
            )
        ],
    )

    book = ledger_service.ledger_from_portfolio(source, source_snapshot_id=7)

    assert book.cash == money("500.00")
    assert book.source_snapshot_id == 7
    assert len(book.positions) == 1
    holding = book.positions[0]
    assert holding.symbol == "GGAL"
    assert holding.quantity == 100
    assert holding.last_price == money("60.56")  # rounded to the cent
    assert holding.average_price == money("50.00")
    assert holding.market == "BCBA"


def test_a_position_without_a_price_is_left_out() -> None:
    source = portfolio(
        positions=[
            Position(instrument=instrument("GGAL"), total_quantity=10, last_price=None),
        ]
    )
    book = ledger_service.ledger_from_portfolio(source)
    assert book.positions == []


def test_a_position_without_a_quantity_is_left_out() -> None:
    source = portfolio(
        positions=[
            Position(instrument=instrument("GGAL"), total_quantity=None, last_price=10.0),
        ]
    )
    book = ledger_service.ledger_from_portfolio(source)
    assert book.positions == []


def test_average_price_falls_back_to_the_last_price() -> None:
    source = portfolio(
        positions=[
            Position(
                instrument=instrument("GGAL"),
                total_quantity=1,
                last_price=20.0,
                average_price=None,
            )
        ]
    )
    book = ledger_service.ledger_from_portfolio(source)
    assert book.positions[0].average_price == money("20.00")


def test_ledger_totals_add_cash_and_positions() -> None:
    book = PaperLedger(
        country="argentina",
        currency="Peso_Argentino",
        cash=money("100.00"),
        positions=[],  # filled below
    )
    source = ledger_service.ledger_from_portfolio(
        portfolio(
            cash="100.00",
            positions=[
                Position(instrument=instrument("GGAL"), total_quantity=2, last_price=50.0),
            ],
        )
    )
    assert source.positions_value == money("100.00")
    assert source.total_value == money("200.00")
    assert book.total_value == money("100.00")


# --- Service level --------------------------------------------------------


async def _connection(db, user, fake):
    return await connections_service.create_connection(
        db,
        user_id=user.id,
        label="My IOL account",
        username=fake.username,
        password=fake.password,
    )


async def test_creating_a_ledger_needs_a_portfolio_snapshot(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await _connection(db_session, user, fake_iol)

    with pytest.raises(ledger_service.LedgerError):
        await ledger_service.create_ledger(
            db_session, user_id=user.id, connection=connection, country="argentina"
        )


async def test_creating_a_ledger_copies_the_saved_portfolio(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await _connection(db_session, user, fake_iol)

    await broker_service.read_account_status(
        db_session, user_id=user.id, connection=connection
    )
    await broker_service.read_portfolio(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    created = await ledger_service.create_ledger(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    # Cash comes from the account-status snapshot (100.00 disponible).
    assert created.cash == money("100.00")
    assert {position.symbol for position in created.positions} == {"GGAL", "YPFD"}
    # 100 GGAL at 60.56 plus 10 YPFD at 1200.00 equals 18056.00.
    assert created.positions_value == money("18056.00")
    assert created.total_value == money("18156.00")

    loaded = await ledger_service.load_ledger(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    assert loaded is not None
    assert loaded.total_value == created.total_value


async def test_loading_a_ledger_without_one_returns_none(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await _connection(db_session, user, fake_iol)

    loaded = await ledger_service.load_ledger(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    assert loaded is None
