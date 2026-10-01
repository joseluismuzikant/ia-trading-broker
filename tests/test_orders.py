"""Tests for saved orders and their idempotency keys."""

from __future__ import annotations

from app.domain.money import money, quantity
from app.domain.trading import Order
from app.services import analysis as analysis_service
from app.services import orders as orders_service
from tests.day5_helpers import make_connection


def an_order(symbol: str = "GGAL", side: str = "BUY", qty: str = "10") -> Order:
    limit = money("100.00")
    return Order(
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        quantity=quantity(qty),
        limit_price=limit,
        notional=money(limit.amount * quantity(qty)),
    )


def test_the_key_is_stable_and_case_insensitive() -> None:
    assert orders_service.idempotency_key(7, "ggal", "BUY") == "proposal:7:BUY:GGAL"


async def _proposal(db, user, connection):
    from datetime import UTC, datetime

    from app.domain.trading import Proposal

    proposal = Proposal(
        country="argentina",
        currency="Peso_Argentino",
        portfolio_value=money("1000.00"),
        cash=money("1000.00"),
        summary="plan",
        created_at=datetime.now(UTC),
    )
    return await analysis_service.save_proposal(
        db, user_id=user.id, connection_id=connection.id, proposal=proposal
    )


async def test_preparing_an_order_is_idempotent(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _proposal(db_session, user, connection)

    first, created_first = await orders_service.prepare_order(
        db_session,
        user_id=user.id,
        connection_id=connection.id,
        proposal_id=record.id,
        country="argentina",
        order=an_order(),
    )
    second, created_second = await orders_service.prepare_order(
        db_session,
        user_id=user.id,
        connection_id=connection.id,
        proposal_id=record.id,
        country="argentina",
        order=an_order(),
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert first.status == "prepared"


async def test_a_saved_order_round_trips(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _proposal(db_session, user, connection)
    saved, _ = await orders_service.prepare_order(
        db_session,
        user_id=user.id,
        connection_id=connection.id,
        proposal_id=record.id,
        country="argentina",
        order=an_order(),
    )

    loaded = orders_service.order_from_record(saved)
    assert loaded.symbol == "GGAL"
    assert loaded.side == "BUY"
    assert loaded.quantity == quantity("10")
    assert loaded.limit_price == money("100.00")


async def test_orders_are_scoped_to_the_user(db_session, make_user, fake_iol) -> None:
    owner = await make_user("alice")
    other = await make_user("mallory")
    connection = await make_connection(db_session, owner, fake_iol)
    record = await _proposal(db_session, owner, connection)
    saved, _ = await orders_service.prepare_order(
        db_session,
        user_id=owner.id,
        connection_id=connection.id,
        proposal_id=record.id,
        country="argentina",
        order=an_order(),
    )

    assert await orders_service.get_order(
        db_session, user_id=owner.id, order_id=saved.id
    ) is not None
    assert await orders_service.get_order(
        db_session, user_id=other.id, order_id=saved.id
    ) is None
    assert await orders_service.count_orders(db_session, user_id=other.id) == 0


async def test_listing_orders_for_a_proposal(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _proposal(db_session, user, connection)
    for symbol in ("GGAL", "YPFD"):
        await orders_service.prepare_order(
            db_session,
            user_id=user.id,
            connection_id=connection.id,
            proposal_id=record.id,
            country="argentina",
            order=an_order(symbol=symbol),
        )

    listed = await orders_service.list_orders_for_proposal(
        db_session, user_id=user.id, proposal_id=record.id
    )
    assert {record.symbol for record in listed} == {"GGAL", "YPFD"}
