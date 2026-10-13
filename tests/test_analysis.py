"""Tests for the analysis service: indicator evidence and the saved proposal."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.infrastructure.iol.schemas import Quote
from app.services import analysis as analysis_service
from app.services import broker as broker_service
from app.services import connections as connections_service
from tests.day5_helpers import buy_proposal


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


# --- Indicator evidence ---------------------------------------------------


def test_evidence_reads_the_trend_from_price_history() -> None:
    evidence = analysis_service.evidence_for(uptrend_quotes())
    assert evidence.observations == 45
    assert evidence.ema_fast is not None and evidence.ema_slow is not None
    assert evidence.ema_fast > evidence.ema_slow
    assert evidence.rsi is not None and evidence.rsi < 70.0
    assert evidence.macd_histogram is not None and evidence.macd_histogram > 0
    assert evidence.last_price is not None


def test_evidence_is_ordered_regardless_of_input_order() -> None:
    quotes = uptrend_quotes()
    forward = analysis_service.evidence_for(quotes)
    backward = analysis_service.evidence_for(list(reversed(quotes)))
    assert forward.last_price == backward.last_price
    assert forward.ema_fast == pytest.approx(backward.ema_fast)


def test_a_short_history_produces_no_readings() -> None:
    evidence = analysis_service.evidence_for(uptrend_quotes(count=5))
    assert evidence.ema_fast is None
    assert evidence.ema_slow is None
    assert evidence.rsi is None
    assert evidence.macd_histogram is None


def test_an_empty_history_is_handled() -> None:
    evidence = analysis_service.evidence_for([])
    assert evidence.observations == 0
    assert evidence.last_price is None


# --- Saving and reading a proposal ----------------------------------------


async def _connection(db, user, fake):
    return await connections_service.create_connection(
        db,
        user_id=user.id,
        label="My IOL account",
        username=fake.username,
        password=fake.password,
    )


async def test_a_history_read_normalises_a_lowercase_market(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await _connection(db_session, user, fake_iol)

    # The market copied from a portfolio payload is lowercased, but IOL's quote
    # path needs the documented uppercase code.
    result = await broker_service.read_price_history(
        db_session,
        user_id=user.id,
        connection=connection,
        market="bcba",
        symbol="GGAL",
    )

    assert result.ok is True
    assert result.snapshot is not None
    assert result.snapshot.payload["quotes"]


async def _save(db, user, connection):
    return await analysis_service.save_proposal(
        db, user_id=user.id, connection_id=connection.id, proposal=buy_proposal()
    )


async def test_a_saved_proposal_is_pending_review(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await _connection(db_session, user, fake_iol)

    record = await _save(db_session, user, connection)

    assert record.status == "pending_review"
    assert record.approval_mode == "HUMAN_IN_THE_LOOP"
    assert record.country == "argentina"
    assert record.payload["summary"]
    assert record.payload["recommendations"]


async def test_a_saved_proposal_is_final(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await _connection(db_session, user, fake_iol)

    first = await _save(db_session, user, connection)
    second = await _save(db_session, user, connection)

    # A later analysis inserts a new row; it never edits the earlier one.
    assert first.id != second.id
    assert await analysis_service.count_proposals(db_session, user_id=user.id) == 2


async def test_a_proposal_is_not_visible_to_another_user(db_session, make_user, fake_iol) -> None:
    owner = await make_user("alice")
    other = await make_user("mallory")
    connection = await _connection(db_session, owner, fake_iol)
    record = await _save(db_session, owner, connection)

    assert (
        await analysis_service.get_proposal(db_session, user_id=owner.id, proposal_id=record.id)
        is not None
    )
    assert (
        await analysis_service.get_proposal(db_session, user_id=other.id, proposal_id=record.id)
        is None
    )


async def test_listing_proposals_is_scoped_to_the_user_and_country(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await _connection(db_session, user, fake_iol)
    await _save(db_session, user, connection)

    listed = await analysis_service.list_proposals(
        db_session, user_id=user.id, connection_id=connection.id, country="argentina"
    )
    assert len(listed) == 1
    empty = await analysis_service.list_proposals(
        db_session, user_id=user.id, connection_id=connection.id, country="estados_unidos"
    )
    assert empty == []
