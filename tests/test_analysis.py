"""Tests for the analysis service: indicators, the plan, and the saved proposal."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.domain.money import money, quantity
from app.domain.trading import PaperLedger, PaperPosition
from app.infrastructure.iol.schemas import Quote
from app.services import analysis as analysis_service
from app.services import broker as broker_service
from app.services import connections as connections_service
from app.services import ledger as ledger_service


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


def flat_ledger(cash: str = "100000.00") -> PaperLedger:
    return PaperLedger(
        country="argentina",
        currency="Peso_Argentino",
        cash=money(cash),
        positions=[],
    )


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


# --- Building a proposal --------------------------------------------------


def test_a_proposal_sizes_a_buy_for_an_allowed_instrument() -> None:
    proposal = analysis_service.build_proposal(
        ledger=flat_ledger(),
        history={"GGAL": uptrend_quotes()},
    )
    assert proposal.country == "argentina"
    assert proposal.cash == money("100000.00")
    assert len(proposal.recommendations) == 1
    ggal = proposal.recommendations[0]
    assert ggal.symbol == "GGAL"
    assert ggal.action == "BUY"
    assert ggal.is_order is True
    assert ggal.quantity > 0
    assert proposal.orders == [ggal]
    assert "Proposed" in proposal.summary
    assert proposal.turnover > 0
    assert all(check.passed for check in proposal.risk_checks)


def test_a_proposal_holds_when_there_is_no_history() -> None:
    proposal = analysis_service.build_proposal(ledger=flat_ledger(), history={})
    assert proposal.recommendations == []
    assert proposal.orders == []
    assert "No trade" in proposal.summary


def test_an_unpriced_holding_is_held_not_traded() -> None:
    book = PaperLedger(
        country="argentina",
        currency="Peso_Argentino",
        cash=money("0.00"),
        positions=[
            PaperPosition(
                symbol="GGAL",
                quantity=quantity("10"),
                average_price=money("100.00"),
                last_price=money("100.00"),
                market="BCBA",
            )
        ],
    )
    proposal = analysis_service.build_proposal(ledger=book, history={"GGAL": []})
    assert proposal.recommendations[0].action == "HOLD"


# --- Saving and reading a proposal ----------------------------------------


async def _connection(db, user, fake):
    return await connections_service.create_connection(
        db,
        user_id=user.id,
        label="My IOL account",
        username=fake.username,
        password=fake.password,
    )


async def _prepare(db, user, connection):
    """Save an account status, a portfolio, a ledger, and price history."""
    await broker_service.read_account_status(db, user_id=user.id, connection=connection)
    await broker_service.read_portfolio(
        db, user_id=user.id, connection=connection, country="argentina"
    )
    await ledger_service.create_ledger(
        db, user_id=user.id, connection=connection, country="argentina"
    )
    for symbol in ("GGAL", "YPFD"):
        await broker_service.read_price_history(
            db,
            user_id=user.id,
            connection=connection,
            market="BCBA",
            symbol=symbol,
        )


async def test_running_an_analysis_needs_a_ledger(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await _connection(db_session, user, fake_iol)

    with pytest.raises(analysis_service.AnalysisError):
        await analysis_service.run_analysis(
            db_session, user_id=user.id, connection=connection, country="argentina"
        )


async def test_running_an_analysis_saves_a_pending_proposal(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await _connection(db_session, user, fake_iol)
    await _prepare(db_session, user, connection)

    record = await analysis_service.run_analysis(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    assert record.status == "pending_review"
    assert record.approval_mode == "HUMAN_IN_THE_LOOP"
    assert record.execution_mode == "PAPER"
    assert record.country == "argentina"
    assert record.payload["summary"]
    assert record.payload["recommendations"]


async def test_a_saved_proposal_is_final(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await _connection(db_session, user, fake_iol)
    await _prepare(db_session, user, connection)

    first = await analysis_service.run_analysis(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    second = await analysis_service.run_analysis(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    # A later analysis inserts a new row; it never edits the earlier one.
    assert first.id != second.id
    assert await analysis_service.count_proposals(db_session, user_id=user.id) == 2


async def test_a_proposal_is_not_visible_to_another_user(
    db_session, make_user, fake_iol
) -> None:
    owner = await make_user("alice")
    other = await make_user("mallory")
    connection = await _connection(db_session, owner, fake_iol)
    await _prepare(db_session, owner, connection)
    record = await analysis_service.run_analysis(
        db_session, user_id=owner.id, connection=connection, country="argentina"
    )

    assert await analysis_service.get_proposal(
        db_session, user_id=owner.id, proposal_id=record.id
    ) is not None
    assert await analysis_service.get_proposal(
        db_session, user_id=other.id, proposal_id=record.id
    ) is None


async def test_listing_proposals_is_scoped_to_the_user_and_country(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await _connection(db_session, user, fake_iol)
    await _prepare(db_session, user, connection)
    await analysis_service.run_analysis(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    listed = await analysis_service.list_proposals(
        db_session, user_id=user.id, connection_id=connection.id, country="argentina"
    )
    assert len(listed) == 1
    empty = await analysis_service.list_proposals(
        db_session, user_id=user.id, connection_id=connection.id, country="estados_unidos"
    )
    assert empty == []
