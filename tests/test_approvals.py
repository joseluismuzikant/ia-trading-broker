"""Tests for the human-approval service: single-use, expiring tokens."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.money import money
from app.domain.trading import Proposal
from app.models import Approval
from app.services import analysis as analysis_service
from app.services import approvals as approvals_service
from tests.day5_helpers import make_connection


async def _proposal(db, user, connection):
    proposal = Proposal(
        country="argentina",
        currency="Peso_Argentino",
        portfolio_value=money("1000.00"),
        cash=money("1000.00"),
        summary="A plan with no recommendations for the approval tests.",
        created_at=datetime.now(UTC),
    )
    return await analysis_service.save_proposal(
        db, user_id=user.id, connection_id=connection.id, proposal=proposal
    )


async def _setup(db, user, fake_iol):
    connection = await make_connection(db, user, fake_iol)
    record = await _proposal(db, user, connection)
    return connection, record


async def test_requesting_an_approval_opens_a_pending_window(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection, record = await _setup(db_session, user, fake_iol)

    approval = await approvals_service.request_approval(
        db_session, user_id=user.id, connection_id=connection.id, proposal_id=record.id
    )

    assert approval.status == "pending"
    assert approval.expires_at > approval.requested_at


async def test_requesting_twice_reuses_the_open_approval(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection, record = await _setup(db_session, user, fake_iol)

    first = await approvals_service.request_approval(
        db_session, user_id=user.id, connection_id=connection.id, proposal_id=record.id
    )
    second = await approvals_service.request_approval(
        db_session, user_id=user.id, connection_id=connection.id, proposal_id=record.id
    )

    assert first.id == second.id


async def test_approving_once_stamps_the_decision(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection, record = await _setup(db_session, user, fake_iol)
    await approvals_service.request_approval(
        db_session, user_id=user.id, connection_id=connection.id, proposal_id=record.id
    )

    approval = await approvals_service.approve(db_session, user_id=user.id, proposal_id=record.id)

    assert approval.status == "approved"
    assert approval.decided_at is not None
    assert approval.decided_by_user_id == user.id


async def test_a_second_approval_is_refused(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection, record = await _setup(db_session, user, fake_iol)
    await approvals_service.request_approval(
        db_session, user_id=user.id, connection_id=connection.id, proposal_id=record.id
    )
    await approvals_service.approve(db_session, user_id=user.id, proposal_id=record.id)

    with pytest.raises(approvals_service.ApprovalNotPendingError):
        await approvals_service.approve(db_session, user_id=user.id, proposal_id=record.id)


async def test_rejecting_records_the_reason(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection, record = await _setup(db_session, user, fake_iol)
    await approvals_service.request_approval(
        db_session, user_id=user.id, connection_id=connection.id, proposal_id=record.id
    )

    approval = await approvals_service.reject(
        db_session, user_id=user.id, proposal_id=record.id, reason="Too risky"
    )

    assert approval.status == "rejected"
    assert approval.reason == "Too risky"


async def test_an_expired_approval_cannot_be_approved(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection, record = await _setup(db_session, user, fake_iol)
    await approvals_service.request_approval(
        db_session,
        user_id=user.id,
        connection_id=connection.id,
        proposal_id=record.id,
        ttl_hours=-1,
    )

    with pytest.raises(approvals_service.ApprovalNotPendingError):
        await approvals_service.approve(db_session, user_id=user.id, proposal_id=record.id)

    latest = await approvals_service.latest_approval(
        db_session, user_id=user.id, proposal_id=record.id
    )
    assert latest is not None
    assert latest.status == "expired"


async def test_consuming_an_approval_is_single_use(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection, record = await _setup(db_session, user, fake_iol)
    await approvals_service.request_approval(
        db_session, user_id=user.id, connection_id=connection.id, proposal_id=record.id
    )
    approval = await approvals_service.approve(db_session, user_id=user.id, proposal_id=record.id)

    consumed = await approvals_service.consume(db_session, approval)
    assert consumed.status == "consumed"
    assert consumed.used_at is not None

    with pytest.raises(approvals_service.ApprovalNotPendingError):
        await approvals_service.consume(db_session, consumed)
    with pytest.raises(approvals_service.ApprovalNotPendingError):
        await approvals_service.approve(db_session, user_id=user.id, proposal_id=record.id)


async def test_an_approval_is_not_visible_to_another_user(db_session, make_user, fake_iol) -> None:
    owner = await make_user("alice")
    other = await make_user("mallory")
    connection, record = await _setup(db_session, owner, fake_iol)
    approval = await approvals_service.request_approval(
        db_session,
        user_id=owner.id,
        connection_id=connection.id,
        proposal_id=record.id,
    )

    assert (
        await approvals_service.get_approval(db_session, user_id=owner.id, approval_id=approval.id)
        is not None
    )
    assert (
        await approvals_service.get_approval(db_session, user_id=other.id, approval_id=approval.id)
        is None
    )


def test_is_expired_only_applies_to_pending() -> None:
    now = datetime.now(UTC)
    past = now - timedelta(hours=1)
    pending = Approval(status="pending", expires_at=past, requested_at=past)
    assert approvals_service.is_expired(pending, now=now) is True
    consumed = Approval(status="consumed", expires_at=past, requested_at=past)
    assert approvals_service.is_expired(consumed, now=now) is False


# --- Execution window: an old approval is not a licence to trade -------------


async def _approved(db, user, fake_iol):
    connection, record = await _setup(db, user, fake_iol)
    await approvals_service.request_approval(
        db, user_id=user.id, connection_id=connection.id, proposal_id=record.id
    )
    return await approvals_service.approve(db, user_id=user.id, proposal_id=record.id)


async def test_a_fresh_approval_may_be_executed(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    approval = await _approved(db_session, user, fake_iol)

    assert approvals_service.execution_problem(approval) is None


async def test_an_approval_older_than_the_window_may_not_be_executed(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    approval = await _approved(db_session, user, fake_iol)
    later = approval.decided_at.replace(tzinfo=UTC) + timedelta(hours=24, minutes=1)

    problem = approvals_service.execution_problem(approval, now=later)

    assert problem is not None
    assert "older than 24 hours" in problem
    assert "new analysis" in problem


async def test_the_window_is_configurable(db_session, make_user, fake_iol, monkeypatch) -> None:
    from app.config import get_settings

    user = await make_user("alice")
    approval = await _approved(db_session, user, fake_iol)
    monkeypatch.setattr(get_settings(), "approval_execution_window_hours", 1)
    ninety_minutes = approval.decided_at.replace(tzinfo=UTC) + timedelta(minutes=90)

    assert approvals_service.execution_problem(approval, now=ninety_minutes) is not None


async def test_a_consumed_approval_also_goes_stale_for_retries(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    approval = await _approved(db_session, user, fake_iol)
    approval = await approvals_service.consume(db_session, approval)
    later = approval.decided_at.replace(tzinfo=UTC) + timedelta(days=3)

    assert approvals_service.execution_problem(approval) is None
    assert approvals_service.execution_problem(approval, now=later) is not None


def test_a_missing_pending_or_rejected_approval_may_not_be_executed() -> None:
    assert "Approve the proposal" in approvals_service.execution_problem(None)
    pending = Approval(status="pending")
    rejected = Approval(status="rejected")
    assert "not approved" in approvals_service.execution_problem(pending)
    assert "not approved" in approvals_service.execution_problem(rejected)


def test_an_approved_approval_without_a_decision_time_fails_closed() -> None:
    assert "no decision time" in approvals_service.execution_problem(Approval(status="approved"))


async def test_refreshing_expires_an_approved_approval_that_was_never_used(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    approval = await _approved(db_session, user, fake_iol)
    later = approval.decided_at.replace(tzinfo=UTC) + timedelta(hours=25)

    refreshed = await approvals_service.refresh_status(db_session, approval, now=later)

    assert refreshed.status == "expired"
    assert "execution window" in refreshed.reason
