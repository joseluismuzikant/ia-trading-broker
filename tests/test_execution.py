"""Tests for paper submission: fills, the ledger, events, and idempotency."""

from __future__ import annotations

import pytest

from app.domain.money import money
from app.domain.trading import OrderResult, Proposal
from app.services import analysis as analysis_service
from app.services import events as events_service
from app.services import execution as execution_service
from app.services import ledger as ledger_service
from app.services import orders as orders_service
from tests.day5_helpers import (
    finalist_for,
    flat_ledger,
    make_connection,
    save_flat_ledger,
    single_symbol_universe,
    uptrend_quotes,
)


class RecordingExecutor:
    """A paper executor that fills everything and remembers the calls."""

    def __init__(self) -> None:
        self.calls: list = []

    async def submit(self, order) -> OrderResult:
        self.calls.append(order)
        return OrderResult(
            status="filled",
            accepted=True,
            filled_quantity=order.quantity,
            filled_price=order.limit_price,
            filled_notional=order.notional,
            message="filled by the test executor",
        )


class UnknownExecutor:
    """An executor that never confirms the order."""

    async def submit(self, order) -> OrderResult:
        return OrderResult(status="unknown", accepted=False, message="no answer")


class RejectingExecutor:
    """An executor that rejects the order."""

    async def submit(self, order) -> OrderResult:
        return OrderResult(status="rejected", accepted=False, message="venue refused")


async def _ready_proposal(db, user, connection):
    proposal = analysis_service.build_proposal(
        ledger=flat_ledger(),
        evidence={"GGAL": analysis_service.evidence_for(uptrend_quotes())},
        finalists=[finalist_for("GGAL")],
        universe=single_symbol_universe(),
    )
    return await analysis_service.save_proposal(
        db, user_id=user.id, connection_id=connection.id, proposal=proposal
    )


async def _prepare(db, user, connection):
    await save_flat_ledger(db, user_id=user.id, connection=connection)
    return await _ready_proposal(db, user, connection)


async def test_submitting_fills_the_orders_and_the_ledger(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _prepare(db_session, user, connection)
    executor = RecordingExecutor()

    report = await execution_service.submit_proposal_orders(
        db_session,
        user_id=user.id,
        connection=connection,
        proposal_record=record,
        executor=executor,
    )

    assert report.filled == 1
    assert len(executor.calls) == 1
    saved = await orders_service.list_orders_for_proposal(
        db_session, user_id=user.id, proposal_id=record.id
    )
    assert saved[0].status == "filled"
    ledger = await ledger_service.load_ledger(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    assert ledger is not None
    assert ledger.cash < money("100000.00")
    assert ledger.position_for("GGAL") is not None


async def test_an_event_is_recorded_for_each_step(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _prepare(db_session, user, connection)

    await execution_service.submit_proposal_orders(
        db_session,
        user_id=user.id,
        connection=connection,
        proposal_record=record,
        executor=RecordingExecutor(),
    )

    event_types = {
        event.event_type
        for event in await events_service.list_events(db_session, user_id=user.id)
    }
    assert "order.prepared" in event_types
    assert "order.filled" in event_types
    assert "ledger.updated" in event_types


async def test_submitting_again_never_resends(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _prepare(db_session, user, connection)
    await execution_service.submit_proposal_orders(
        db_session,
        user_id=user.id,
        connection=connection,
        proposal_record=record,
        executor=RecordingExecutor(),
    )
    ledger_after_first = await ledger_service.load_ledger(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    second_executor = RecordingExecutor()

    report = await execution_service.submit_proposal_orders(
        db_session,
        user_id=user.id,
        connection=connection,
        proposal_record=record,
        executor=second_executor,
    )

    assert report.filled == 0
    assert report.skipped == 1
    assert second_executor.calls == []
    ledger_after_second = await ledger_service.load_ledger(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    assert ledger_after_first is not None and ledger_after_second is not None
    assert ledger_after_first.cash == ledger_after_second.cash


async def test_an_unknown_result_is_not_resubmitted(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _prepare(db_session, user, connection)

    report = await execution_service.submit_proposal_orders(
        db_session,
        user_id=user.id,
        connection=connection,
        proposal_record=record,
        executor=UnknownExecutor(),
    )
    assert report.unknown == 1
    assert report.needs_reconciliation is True

    saved = await orders_service.list_orders_for_proposal(
        db_session, user_id=user.id, proposal_id=record.id
    )
    assert saved[0].status == "unknown"

    # A later pass must not send the unknown order again.
    retry = RecordingExecutor()
    again = await execution_service.submit_proposal_orders(
        db_session,
        user_id=user.id,
        connection=connection,
        proposal_record=record,
        executor=retry,
    )
    assert again.filled == 0
    assert retry.calls == []
    ledger = await ledger_service.load_ledger(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    assert ledger is not None
    assert ledger.cash == money("100000.00")


async def test_an_existing_prepared_order_is_not_resubmitted(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _prepare(db_session, user, connection)
    proposal = Proposal.model_validate(record.payload)
    order = execution_service.order_from_recommendation(
        proposal.orders[0], proposal.execution_mode
    )
    await orders_service.prepare_order(
        db_session,
        user_id=user.id,
        connection_id=connection.id,
        proposal_id=record.id,
        country=proposal.country,
        order=order,
    )
    executor = RecordingExecutor()

    report = await execution_service.submit_proposal_orders(
        db_session,
        user_id=user.id,
        connection=connection,
        proposal_record=record,
        executor=executor,
    )

    assert report.skipped == 1
    assert report.unknown == 1
    assert report.needs_reconciliation is True
    assert executor.calls == []


async def test_live_execution_is_refused(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    await save_flat_ledger(db_session, user_id=user.id, connection=connection)
    record = await _ready_proposal(db_session, user, connection)
    payload = dict(record.payload)
    payload["execution_mode"] = "LIVE"
    record.payload = payload
    await db_session.commit()

    with pytest.raises(execution_service.ExecutionError, match="Live execution"):
        await execution_service.submit_proposal_orders(
            db_session,
            user_id=user.id,
            connection=connection,
            proposal_record=record,
            executor=RecordingExecutor(),
        )


async def test_execution_requires_a_paper_ledger(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _ready_proposal(db_session, user, connection)

    with pytest.raises(execution_service.ExecutionError, match="ledger"):
        await execution_service.submit_proposal_orders(
            db_session,
            user_id=user.id,
            connection=connection,
            proposal_record=record,
            executor=RecordingExecutor(),
        )


async def test_a_rejected_order_does_not_change_the_ledger(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _prepare(db_session, user, connection)

    report = await execution_service.submit_proposal_orders(
        db_session,
        user_id=user.id,
        connection=connection,
        proposal_record=record,
        executor=RejectingExecutor(),
    )

    assert report.rejected == 1
    ledger = await ledger_service.load_ledger(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    assert ledger is not None
    assert ledger.cash == money("100000.00")
