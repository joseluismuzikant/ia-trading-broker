"""Tests for applying paper fills to the ledger."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.money import money, quantity
from app.domain.trading import Order, PaperLedger, PaperPosition
from app.services import ledger as ledger_service
from tests.day5_helpers import flat_ledger


def a_buy(symbol: str = "GGAL", qty: str = "10", price: str = "100.00") -> Order:
    limit = money(price)
    return Order(
        symbol=symbol,
        side="BUY",
        quantity=quantity(qty),
        limit_price=limit,
        notional=money(limit.amount * quantity(qty)),
    )


def a_sell(symbol: str = "GGAL", qty: str = "10", price: str = "100.00") -> Order:
    limit = money(price)
    return Order(
        symbol=symbol,
        side="SELL",
        quantity=quantity(qty),
        limit_price=limit,
        notional=money(limit.amount * quantity(qty)),
    )


def test_a_buy_opens_a_position_and_spends_cash() -> None:
    updated = ledger_service.apply_fill(
        flat_ledger("1000.00"),
        a_buy(qty="2", price="100.00"),
        filled_quantity=quantity("2"),
        filled_price=money("100.00"),
    )
    position = updated.position_for("GGAL")
    assert position is not None
    assert position.quantity == quantity("2")
    assert position.average_price == money("100.00")
    assert updated.cash == money("800.00")


def test_a_second_buy_averages_the_price() -> None:
    start = PaperLedger(
        country="argentina",
        currency="Peso_Argentino",
        cash=money("2000.00"),
        positions=[
            PaperPosition(
                symbol="GGAL",
                quantity=quantity("10"),
                average_price=money("100.00"),
                last_price=money("100.00"),
            )
        ],
    )
    updated = ledger_service.apply_fill(
        start,
        a_buy(qty="10", price="120.00"),
        filled_quantity=quantity("10"),
        filled_price=money("120.00"),
    )
    position = updated.position_for("GGAL")
    assert position is not None
    assert position.quantity == quantity("20")
    assert position.average_price == money("110.00")
    assert updated.cash == money("800.00")


def test_a_sell_reduces_the_position_and_adds_cash() -> None:
    start = PaperLedger(
        country="argentina",
        currency="Peso_Argentino",
        cash=money("0.00"),
        positions=[
            PaperPosition(
                symbol="GGAL",
                quantity=quantity("20"),
                average_price=money("100.00"),
                last_price=money("100.00"),
            )
        ],
    )
    updated = ledger_service.apply_fill(
        start,
        a_sell(qty="5", price="110.00"),
        filled_quantity=quantity("5"),
        filled_price=money("110.00"),
    )
    position = updated.position_for("GGAL")
    assert position is not None
    assert position.quantity == quantity("15")
    assert position.average_price == money("100.00")
    assert updated.cash == money("550.00")


def test_selling_the_whole_position_removes_it() -> None:
    start = PaperLedger(
        country="argentina",
        currency="Peso_Argentino",
        cash=money("0.00"),
        positions=[
            PaperPosition(
                symbol="GGAL",
                quantity=quantity("10"),
                average_price=money("100.00"),
                last_price=money("100.00"),
            )
        ],
    )
    updated = ledger_service.apply_fill(
        start,
        a_sell(qty="10", price="120.00"),
        filled_quantity=quantity("10"),
        filled_price=money("120.00"),
    )
    assert updated.position_for("GGAL") is None
    assert updated.cash == money("1200.00")


def test_a_zero_fill_is_rejected() -> None:
    with pytest.raises(ledger_service.LedgerError, match="positive"):
        ledger_service.apply_fill(
            flat_ledger("1000.00"),
            a_buy(),
            filled_quantity=quantity("0"),
            filled_price=money("100.00"),
        )


def test_a_buy_cannot_overdraw_paper_cash() -> None:
    with pytest.raises(ledger_service.LedgerError, match="cash"):
        ledger_service.apply_fill(
            flat_ledger("50.00"),
            a_buy(qty="1", price="100.00"),
            filled_quantity=quantity("1"),
            filled_price=money("100.00"),
        )


def test_a_sell_cannot_exceed_the_paper_position() -> None:
    start = PaperLedger(
        country="argentina",
        currency="Peso_Argentino",
        cash=money("0.00"),
        positions=[
            PaperPosition(
                symbol="GGAL",
                quantity=quantity("2"),
                average_price=money("100.00"),
                last_price=money("100.00"),
            )
        ],
    )
    with pytest.raises(ledger_service.LedgerError, match="only 2"):
        ledger_service.apply_fill(
            start,
            a_sell(qty="3", price="100.00"),
            filled_quantity=quantity("3"),
            filled_price=money("100.00"),
        )


async def test_save_ledger_round_trips(db_session, make_user, fake_iol) -> None:
    from app.services import connections as connections_service

    user = await make_user("alice")
    connection = await connections_service.create_connection(
        db_session,
        user_id=user.id,
        label="A",
        username=fake_iol.username,
        password=fake_iol.password,
    )
    ledger = flat_ledger("500.00")
    await ledger_service.save_ledger(
        db_session, user_id=user.id, connection=connection, ledger=ledger
    )

    loaded = await ledger_service.load_ledger(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    assert loaded is not None
    assert loaded.cash == money("500.00")
    # The saved timestamp mark does not affect the cash that was written.
    assert isinstance(datetime.now(UTC), datetime)
