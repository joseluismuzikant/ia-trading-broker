"""Tests for the MVP LangGraph workflow: fresh data, one model call, human, live.

These tests describe the active product flow. The fake portfolio advisor stands
in for the model and the fake IOL transport stands in for the broker, so the
whole graph runs offline and no test can ever send a real order.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.trading import Proposal
from app.infrastructure.simulation import advice_from_targets
from app.services import analysis as analysis_service
from app.services import approvals as approvals_service
from app.services import ledger as ledger_service
from app.services import orders as orders_service
from app.workflows import trading_flow
from tests.day5_helpers import make_connection

#: The fixture portfolio: 100 GGAL at 60.555, 10 YPFD at 1200, cash 100.
HELD = {"GGAL", "YPFD"}


async def _orders(db, user, proposal_id):
    return await orders_service.list_orders_for_proposal(
        db, user_id=user.id, proposal_id=proposal_id
    )


async def _ledger(db, user, connection):
    return await ledger_service.load_ledger(
        db, user_id=user.id, connection=connection, country="argentina"
    )


def _enable_live(monkeypatch) -> None:
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "iol_live_plazo", "t2")
    monkeypatch.setattr(settings, "iol_live_validity_days", 1)


# --- Fresh broker state and a new paper ledger -----------------------------


async def test_a_new_analysis_refreshes_the_broker_portfolio(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    state = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    assert state["status"] == trading_flow.STATUS_AWAITING_APPROVAL
    read_paths = fake_iol.authenticated_paths
    assert "/api/v2/estadocuenta" in read_paths
    assert any(path.startswith("/api/v2/portafolio/") for path in read_paths)
    assert any("/Cotizacion/seriehistorica/" in path for path in read_paths)


async def test_a_fresh_paper_ledger_is_created_automatically(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    # An old simulated portfolio exists; it must NOT be reused as the start.
    from tests.day5_helpers import save_flat_ledger

    await save_flat_ledger(db_session, user_id=user.id, connection=connection)
    stale = await _ledger(db_session, user, connection)
    assert stale is not None and not stale.positions

    state = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    assert state["status"] == trading_flow.STATUS_AWAITING_APPROVAL
    ledger = await _ledger(db_session, user, connection)
    assert ledger is not None
    assert {position.symbol for position in ledger.positions} == HELD
    proposal = await analysis_service.get_proposal(
        db_session, user_id=user.id, proposal_id=state["proposal_id"]
    )
    plan = Proposal.model_validate(proposal.payload)
    assert plan.ledger_before is not None
    assert {p.symbol for p in plan.ledger_before.positions} == HELD


async def test_a_run_without_any_broker_portfolio_fails_cleanly(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    fake_iol.portfolio_error_status = 500
    fake_iol.account_status_error_status = 500

    state = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    assert state["status"] == trading_flow.STATUS_FAILED
    assert state["proposal_id"] is None


# --- One model call, and it decides the rebalance --------------------------


async def test_the_whole_portfolio_is_analysed_in_one_model_call(
    db_session, make_user, fake_iol, fake_advisor
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    assert len(fake_advisor.contexts) == 1
    assert fake_advisor.contexts[0].held_symbols == HELD


async def test_the_target_allocation_is_the_source_of_the_orders(
    db_session, make_user, fake_iol, fake_advisor
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    # The model sells the whole GGAL position and buys a new one in VIST.
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )

    state = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_record = await analysis_service.get_proposal(
        db_session, user_id=user.id, proposal_id=state["proposal_id"]
    )
    proposal = Proposal.model_validate(proposal_record.payload)

    sides = [(item.action, item.symbol) for item in proposal.orders]
    assert ("SELL", "GGAL") in sides
    assert ("BUY", "VIST") in sides
    # The legacy funnel no longer decides anything.
    assert proposal.candidates == []
    assert proposal.finalists == []


async def test_the_allocation_stays_self_financing(
    db_session, make_user, fake_iol, fake_advisor
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )

    state = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_record = await analysis_service.get_proposal(
        db_session, user_id=user.id, proposal_id=state["proposal_id"]
    )
    proposal = Proposal.model_validate(proposal_record.payload)

    total = proposal.ledger_before.total_value
    assert proposal.ledger_after.total_value == total
    assert sum(item.target_weight for item in proposal.advice.decisions) + (
        proposal.advice.target_cash_weight
    ) == pytest.approx(1.0)


async def test_the_paper_preview_matches_the_proposed_orders(
    db_session, make_user, fake_iol, fake_advisor
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )

    state = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_record = await analysis_service.get_proposal(
        db_session, user_id=user.id, proposal_id=state["proposal_id"]
    )
    proposal = Proposal.model_validate(proposal_record.payload)

    expected = {(item.symbol, item.action): Decimal(item.quantity) for item in proposal.orders}
    before = {p.symbol: p.quantity for p in proposal.ledger_before.positions}
    after = {p.symbol: p.quantity for p in proposal.ledger_after.positions}
    for (symbol, side), units in expected.items():
        if side == "BUY":
            assert after.get(symbol, Decimal("0")) - before.get(symbol, Decimal("0")) == units
        else:
            assert before.get(symbol, Decimal("0")) - after.get(symbol, Decimal("0")) == units


async def test_sells_are_sequenced_before_buys(
    db_session, make_user, fake_iol, fake_advisor
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )

    state = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_record = await analysis_service.get_proposal(
        db_session, user_id=user.id, proposal_id=state["proposal_id"]
    )
    proposal = Proposal.model_validate(proposal_record.payload)

    sides = [item.action for item in proposal.orders]
    assert sides == sorted(sides, key=lambda side: 0 if side == "SELL" else 1)


# --- Human review before any live order -----------------------------------


async def test_the_review_pauses_before_live_execution(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    state = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    assert state["status"] == trading_flow.STATUS_AWAITING_APPROVAL
    assert state["paused"] is True
    assert await _orders(db_session, user, state["proposal_id"]) == []
    assert fake_iol.trade_posts == []


async def test_rejecting_sends_nothing_to_the_broker(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    started = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_id = started["proposal_id"]

    await approvals_service.reject(db_session, user_id=user.id, proposal_id=proposal_id)
    resumed = await trading_flow.resume_run(db_session, user_id=user.id, proposal_id=proposal_id)

    assert resumed["status"] == trading_flow.STATUS_REJECTED
    assert await _orders(db_session, user, proposal_id) == []
    assert fake_iol.trade_posts == []


async def test_approving_without_trading_terms_sends_nothing(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    started = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_id = started["proposal_id"]

    await approvals_service.approve(db_session, user_id=user.id, proposal_id=proposal_id)
    resumed = await trading_flow.resume_run(db_session, user_id=user.id, proposal_id=proposal_id)

    assert resumed["status"] == trading_flow.STATUS_BLOCKED
    assert "IOL_LIVE_PLAZO is not configured" in (resumed["message"] or "")
    assert await _orders(db_session, user, proposal_id) == []
    assert fake_iol.trade_posts == []


# --- Live execution -------------------------------------------------------


async def test_approving_sends_the_sales_then_the_purchases(
    db_session, make_user, fake_iol, fake_advisor, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )
    started = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_id = started["proposal_id"]

    await approvals_service.approve(db_session, user_id=user.id, proposal_id=proposal_id)
    resumed = await trading_flow.resume_run(
        db_session, user_id=user.id, proposal_id=proposal_id, phase="sells"
    )

    assert resumed["status"] == trading_flow.STATUS_COMPLETED
    # PHASE 1: only the sales go out.
    assert [path for path, _ in fake_iol.trade_posts] == [
        "/api/v2/operar/Vender",
        "/api/v2/operar/Vender",
    ]
    approval = await approvals_service.latest_approval(
        db_session, user_id=user.id, proposal_id=proposal_id
    )
    assert approval is not None and approval.status == "approved"

    # PHASE 2: the purchases, against the cash the broker actually reports.
    fake_iol.account_status["cuentas"][0]["disponible"] = 100_000.0
    resumed = await trading_flow.resume_run(
        db_session, user_id=user.id, proposal_id=proposal_id, phase="buys"
    )

    assert resumed["status"] == trading_flow.STATUS_COMPLETED
    assert [path for path, _ in fake_iol.trade_posts] == [
        "/api/v2/operar/Vender",
        "/api/v2/operar/Vender",
        "/api/v2/operar/Comprar",
    ]
    orders = await _orders(db_session, user, proposal_id)
    assert all(order.status == "accepted" for order in orders)
    assert all(order.broker_order_id for order in orders)
    approval = await approvals_service.latest_approval(
        db_session, user_id=user.id, proposal_id=proposal_id
    )
    assert approval is not None and approval.status == "consumed"
    record = await analysis_service.get_proposal(
        db_session, user_id=user.id, proposal_id=proposal_id
    )
    assert record is not None and record.status == "approved"


async def test_purchases_are_deferred_when_the_broker_reports_no_cash(
    db_session, make_user, fake_iol, fake_advisor, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )
    started = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_id = started["proposal_id"]
    await approvals_service.approve(db_session, user_id=user.id, proposal_id=proposal_id)
    await trading_flow.resume_run(
        db_session, user_id=user.id, proposal_id=proposal_id, phase="sells"
    )
    sent_sales = len(fake_iol.trade_posts)

    # The fixture reports 100 pesos of available cash: nothing is affordable.
    resumed = await trading_flow.resume_run(
        db_session, user_id=user.id, proposal_id=proposal_id, phase="buys"
    )

    assert resumed["status"] == trading_flow.STATUS_COMPLETED
    assert "deferred" in (resumed["message"] or "")
    assert len(fake_iol.trade_posts) == sent_sales
    # The approval stays open so the purchases can be sent when cash arrives.
    approval = await approvals_service.latest_approval(
        db_session, user_id=user.id, proposal_id=proposal_id
    )
    assert approval is not None and approval.status == "approved"


async def test_a_repeated_phase_never_resends_orders(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    started = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_id = started["proposal_id"]
    await approvals_service.approve(db_session, user_id=user.id, proposal_id=proposal_id)
    await trading_flow.resume_run(
        db_session, user_id=user.id, proposal_id=proposal_id, phase="sells"
    )
    sent = len(fake_iol.trade_posts)

    again = await trading_flow.resume_run(
        db_session, user_id=user.id, proposal_id=proposal_id, phase="sells"
    )

    assert again["status"] == trading_flow.STATUS_COMPLETED
    assert len(fake_iol.trade_posts) == sent


async def test_live_execution_never_touches_the_paper_ledger(
    db_session, make_user, fake_iol, fake_advisor, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )
    started = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    before_execution = await _ledger(db_session, user, connection)

    await approvals_service.approve(db_session, user_id=user.id, proposal_id=started["proposal_id"])
    await trading_flow.resume_run(db_session, user_id=user.id, proposal_id=started["proposal_id"])

    after_execution = await _ledger(db_session, user, connection)
    assert after_execution is not None and before_execution is not None
    assert after_execution.cash == before_execution.cash
    assert {p.symbol: p.quantity for p in after_execution.positions} == {
        p.symbol: p.quantity for p in before_execution.positions
    }


async def test_a_second_approval_or_restart_never_resends(
    db_session, make_user, fake_iol, fake_advisor, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    started = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_id = started["proposal_id"]

    await approvals_service.approve(db_session, user_id=user.id, proposal_id=proposal_id)
    await trading_flow.resume_run(db_session, user_id=user.id, proposal_id=proposal_id)
    sent = len(fake_iol.trade_posts)

    # A second decision is refused, and a restart sends nothing new.
    with pytest.raises(approvals_service.ApprovalNotPendingError):
        await approvals_service.approve(db_session, user_id=user.id, proposal_id=proposal_id)
    again = await trading_flow.resume_run(db_session, user_id=user.id, proposal_id=proposal_id)

    assert again["status"] == trading_flow.STATUS_COMPLETED
    assert len(fake_iol.trade_posts) == sent


async def test_an_unknown_order_is_never_resent(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    started = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_id = started["proposal_id"]
    # The first trading POST times out: the outcome cannot be proven.
    fake_iol.trade_responses.append({"raise": "timeout"})

    await approvals_service.approve(db_session, user_id=user.id, proposal_id=proposal_id)
    resumed = await trading_flow.resume_run(db_session, user_id=user.id, proposal_id=proposal_id)
    sent = len(fake_iol.trade_posts)

    assert resumed["status"] == trading_flow.STATUS_BLOCKED
    orders = await _orders(db_session, user, proposal_id)
    assert orders and orders[0].status == "unknown"

    again = await trading_flow.resume_run(db_session, user_id=user.id, proposal_id=proposal_id)
    assert again["status"] == trading_flow.STATUS_BLOCKED
    assert len(fake_iol.trade_posts) == sent


async def test_resuming_a_finished_run_changes_nothing(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    started = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    proposal_id = started["proposal_id"]
    await approvals_service.approve(db_session, user_id=user.id, proposal_id=proposal_id)
    await trading_flow.resume_run(db_session, user_id=user.id, proposal_id=proposal_id)
    orders_after = await _orders(db_session, user, proposal_id)

    again = await trading_flow.resume_run(db_session, user_id=user.id, proposal_id=proposal_id)

    assert again["status"] == trading_flow.STATUS_COMPLETED
    assert len(await _orders(db_session, user, proposal_id)) == len(orders_after)


# --- Sending an answer back: the retry edge ---------------------------------

from dataclasses import dataclass, field  # noqa: E402

from app.infrastructure.simulation import FakePortfolioAdvisor  # noqa: E402
from app.services import advice as advice_service  # noqa: E402
from app.services import events as events_service  # noqa: E402

CURRENT = {"GGAL": 0.3336, "YPFD": 0.6609}


def _fillable():
    """Sell some GGAL, keep YPFD: every trade can be filled with whole units."""
    return advice_from_targets({"GGAL": 0.30, "YPFD": 0.60}, cash=0.10, current=CURRENT)


def _unfillable():
    """A new VIST position worth about 109 pesos when one VIST costs about 128.

    The target is above the allocation tolerance, so it is a real BUY request,
    yet below one whole share, so it can never be filled.
    """
    return advice_from_targets(
        {"GGAL": 0.30, "YPFD": 0.594, "VIST": 0.006}, cash=0.10, current=CURRENT
    )


def _invented_symbol():
    """Allocates money to a symbol that is not in the account or the watchlist."""
    return advice_from_targets(
        {"GGAL": 0.30, "YPFD": 0.594, "ZZZZ": 0.006}, cash=0.10, current=CURRENT
    )


@dataclass
class ScriptedAdvisor(FakePortfolioAdvisor):
    """Answers from a list, one per call, and remembers every context it saw."""

    answers: list = field(default_factory=list)

    async def analyze(self, context):
        self.contexts.append(context)
        index = min(len(self.contexts), len(self.answers)) - 1
        return self.answers[index]


def _use(monkeypatch, *answers) -> ScriptedAdvisor:
    advisor = ScriptedAdvisor(answers=list(answers))
    monkeypatch.setattr(advice_service, "build_advisor", lambda model=None: advisor)
    return advisor


async def _run(db_session, make_user, fake_iol):
    from tests.day5_helpers import save_price_history_snapshot, uptrend_quotes

    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    # VIST has a price (about 128 pesos a share), so a tiny target is a real,
    # unfillable request rather than a symbol the planner skips.
    await save_price_history_snapshot(
        db_session,
        user_id=user.id,
        connection=connection,
        symbol="VIST",
        quotes=uptrend_quotes(),
    )
    state = await trading_flow.start_run(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    return user, connection, state


async def _proposal_of(db_session, user, state) -> Proposal:
    record = await analysis_service.get_proposal(
        db_session, user_id=user.id, proposal_id=state["proposal_id"]
    )
    return Proposal.model_validate(record.payload)


async def _event_types(db_session, user):
    events = await events_service.list_events(db_session, user_id=user.id)
    return [event.event_type for event in events]


async def test_an_unfillable_answer_is_sent_back_and_the_second_one_is_used(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    advisor = _use(monkeypatch, _unfillable(), _fillable())

    user, _connection, state = await _run(db_session, make_user, fake_iol)

    assert state["status"] == trading_flow.STATUS_AWAITING_APPROVAL
    assert len(advisor.contexts) == 2
    assert advisor.contexts[0].corrections == ()
    corrections = advisor.contexts[1].corrections
    assert corrections and "VIST" in corrections[0]
    assert "does not fit" in corrections[0]
    proposal = await _proposal_of(db_session, user, state)
    assert proposal.attempts == 2
    assert [item.symbol for item in proposal.recommendations if not item.executable] == []
    assert any("sent back 1 time" in warning for warning in proposal.warnings)
    assert "advice.retry" in await _event_types(db_session, user)


async def test_a_refused_answer_is_sent_back_before_the_run_gives_up(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    advisor = _use(monkeypatch, _invented_symbol(), _fillable())

    user, _connection, state = await _run(db_session, make_user, fake_iol)

    assert state["status"] == trading_flow.STATUS_AWAITING_APPROVAL
    assert len(advisor.contexts) == 2
    assert "ZZZZ" in advisor.contexts[1].corrections[0]
    assert (await _proposal_of(db_session, user, state)).attempts == 2


async def test_two_refusals_in_a_row_stop_the_run_without_a_proposal(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    advisor = _use(monkeypatch, _invented_symbol(), _invented_symbol())

    user, _connection, state = await _run(db_session, make_user, fake_iol)

    assert state["status"] == trading_flow.STATUS_FAILED
    assert len(advisor.contexts) == 2  # bounded: never a third call
    assert await analysis_service.count_proposals(db_session, user_id=user.id) == 0


async def test_when_attempts_run_out_the_proposal_still_lists_what_cannot_be_traded(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    advisor = _use(monkeypatch, _unfillable())  # the model never corrects itself

    user, _connection, state = await _run(db_session, make_user, fake_iol)

    assert state["status"] == trading_flow.STATUS_AWAITING_APPROVAL
    assert len(advisor.contexts) == 2
    proposal = await _proposal_of(db_session, user, state)
    assert proposal.attempts == 2
    blocked = [item for item in proposal.recommendations if not item.executable]
    assert [item.symbol for item in blocked] == ["VIST"]
    assert any("still cannot be traded" in warning for warning in proposal.warnings)


async def test_one_attempt_disables_the_retry(db_session, make_user, fake_iol, monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "analysis_max_attempts", 1)
    advisor = _use(monkeypatch, _unfillable(), _fillable())

    user, _connection, state = await _run(db_session, make_user, fake_iol)

    assert len(advisor.contexts) == 1
    assert (await _proposal_of(db_session, user, state)).attempts == 1


async def test_a_fillable_first_answer_is_never_sent_back(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    advisor = _use(monkeypatch, _fillable())

    user, _connection, state = await _run(db_session, make_user, fake_iol)

    assert len(advisor.contexts) == 1
    assert (await _proposal_of(db_session, user, state)).attempts == 1
    assert "advice.retry" not in await _event_types(db_session, user)
