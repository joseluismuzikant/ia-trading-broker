"""Tests for the Day 5 LangGraph workflow: pause, resume, and idempotency."""

from __future__ import annotations

from app.services import analysis as analysis_service
from app.services import approvals as approvals_service
from app.services import events as events_service
from app.services import ledger as ledger_service
from app.services import orders as orders_service
from app.workflows import trading_flow
from tests.day5_helpers import (
    make_connection,
    prepare_tradeable_account,
    save_price_history_snapshot,
    uptrend_quotes,
)


async def _ready(db, user, fake_iol):
    connection = await make_connection(db, user, fake_iol)
    await prepare_tradeable_account(db, user=user, connection=connection)
    return connection


async def _orders(db, user, proposal_id):
    return await orders_service.list_orders_for_proposal(
        db, user_id=user.id, proposal_id=proposal_id
    )


async def test_a_run_pauses_for_approval(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await _ready(db_session, user, fake_iol)

    state = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    assert state["status"] == trading_flow.STATUS_AWAITING_APPROVAL
    assert state["paused"] is True
    assert state["proposal_id"] is not None
    approval = await approvals_service.latest_approval(
        db_session, user_id=user.id, proposal_id=state["proposal_id"]
    )
    assert approval is not None and approval.status == "pending"
    # Nothing is sent while the plan waits.
    assert await _orders(db_session, user, state["proposal_id"]) == []


async def test_a_run_without_a_ledger_fails_cleanly(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    state = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    assert state["status"] == trading_flow.STATUS_FAILED
    assert state["proposal_id"] is None


async def test_approving_then_resuming_fills_exactly_once(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await _ready(db_session, user, fake_iol)
    started = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_id = started["proposal_id"]

    await approvals_service.approve(db_session, user_id=user.id, proposal_id=proposal_id)
    resumed = await trading_flow.resume_run(
        db_session, user_id=user.id, proposal_id=proposal_id
    )

    assert resumed["status"] == trading_flow.STATUS_COMPLETED
    orders = await _orders(db_session, user, proposal_id)
    assert len(orders) == 2
    assert all(order.status == "filled" for order in orders)
    approval = await approvals_service.latest_approval(
        db_session, user_id=user.id, proposal_id=proposal_id
    )
    assert approval is not None and approval.status == "consumed"
    record = await analysis_service.get_proposal(
        db_session, user_id=user.id, proposal_id=proposal_id
    )
    assert record is not None and record.status == "approved"


async def test_resuming_a_finished_run_changes_nothing(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await _ready(db_session, user, fake_iol)
    started = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_id = started["proposal_id"]
    await approvals_service.approve(db_session, user_id=user.id, proposal_id=proposal_id)
    await trading_flow.resume_run(db_session, user_id=user.id, proposal_id=proposal_id)
    orders_after = await _orders(db_session, user, proposal_id)
    ledger_after = await ledger_service.load_ledger(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    again = await trading_flow.resume_run(
        db_session, user_id=user.id, proposal_id=proposal_id
    )

    assert again["status"] == trading_flow.STATUS_COMPLETED
    assert again["current_node"] == "already_executed"
    assert len(orders_after) >= 1
    assert len(await _orders(db_session, user, proposal_id)) == len(orders_after)
    ledger_now = await ledger_service.load_ledger(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    assert ledger_after is not None and ledger_now is not None
    assert ledger_after.cash == ledger_now.cash


async def test_resuming_a_pending_run_stays_paused(
    db_session, make_user, fake_iol
) -> None:
    """A restart between the request and the decision changes nothing."""
    user = await make_user("alice")
    connection = await _ready(db_session, user, fake_iol)
    started = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_id = started["proposal_id"]

    restarted = await trading_flow.resume_run(
        db_session, user_id=user.id, proposal_id=proposal_id
    )

    assert restarted["status"] == trading_flow.STATUS_AWAITING_APPROVAL
    assert restarted["paused"] is True
    assert await _orders(db_session, user, proposal_id) == []


async def test_rejecting_closes_the_run_without_an_order(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await _ready(db_session, user, fake_iol)
    started = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_id = started["proposal_id"]

    await approvals_service.reject(
        db_session, user_id=user.id, proposal_id=proposal_id, reason="Not now"
    )
    resumed = await trading_flow.resume_run(
        db_session, user_id=user.id, proposal_id=proposal_id
    )

    assert resumed["status"] == trading_flow.STATUS_REJECTED
    assert await _orders(db_session, user, proposal_id) == []
    record = await analysis_service.get_proposal(
        db_session, user_id=user.id, proposal_id=proposal_id
    )
    assert record is not None and record.status == "rejected"


async def test_a_price_move_blocks_the_approved_plan(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await _ready(db_session, user, fake_iol)
    started = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_id = started["proposal_id"]
    await approvals_service.approve(db_session, user_id=user.id, proposal_id=proposal_id)

    # A large move in the instrument's saved price, after the review.
    await save_price_history_snapshot(
        db_session,
        user_id=user.id,
        connection=connection,
        symbol="GGAL",
        quotes=uptrend_quotes(start=200.0),
    )
    resumed = await trading_flow.resume_run(
        db_session, user_id=user.id, proposal_id=proposal_id
    )

    assert resumed["status"] == trading_flow.STATUS_BLOCKED
    assert await _orders(db_session, user, proposal_id) == []
    event_types = {
        event.event_type
        for event in await events_service.list_events(db_session, user_id=user.id)
    }
    assert "run.blocked" in event_types


async def test_a_second_run_saves_a_second_proposal(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await _ready(db_session, user, fake_iol)

    first = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    second = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    assert first["proposal_id"] != second["proposal_id"]
    assert await analysis_service.count_proposals(db_session, user_id=user.id) == 2
