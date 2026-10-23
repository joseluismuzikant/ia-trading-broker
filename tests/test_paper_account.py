"""Tests for paper accounts: a simulated account that reads like a broker.

A paper account has its own saved state, takes prices from a real connection,
and its trades never reach a broker. Everything runs against the fake IOL, so no
real call or order is possible.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.money import money
from app.domain.trading import PaperLedger, Proposal
from app.infrastructure.simulation import PaperBroker, advice_from_targets
from app.ports.broker_reader import BrokerError, BrokerReader, BrokerUnavailableError, PriceBar
from app.services import analysis as analysis_service
from app.services import broker as broker_service
from app.services import connections as connections_service
from app.services import execution as execution_service
from app.services import ledger as ledger_service
from app.workflows import trading_flow
from tests.day5_helpers import make_connection
from tests.helpers import extract_csrf, login


async def _source_with_portfolio(db, user, fake_iol):
    """A real (fake-IOL) connection with an account status and a portfolio saved."""
    source = await make_connection(db, user, fake_iol)
    await broker_service.read_account_status(db, user_id=user.id, connection=source)
    await broker_service.read_portfolio(db, user_id=user.id, connection=source, country="argentina")
    return source


async def _paper(db, user, source, *, starting="copy", cash="", label="My paper account"):
    connection = await connections_service.create_broker_connection(
        db,
        user_id=user.id,
        broker="paper",
        label=label,
        fields={},
        options={"starting": starting, "initial_cash": cash},
        price_source_connection_id=source.id,
    )
    await ledger_service.reset_paper_account(db, user_id=user.id, connection=connection)
    return connection


# --- Starting and resetting the account ----------------------------------------


async def test_a_paper_account_can_start_with_cash(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    source = await _source_with_portfolio(db_session, user, fake_iol)

    paper = await _paper(db_session, user, source, starting="cash", cash="250,000.50")

    account = await ledger_service.load_paper_account(
        db_session, user_id=user.id, connection=paper, country="argentina"
    )
    assert account.cash == money("250000.50")
    assert account.positions == []


async def test_a_paper_account_can_copy_the_holdings_of_its_price_source(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    source = await _source_with_portfolio(db_session, user, fake_iol)

    paper = await _paper(db_session, user, source, starting="copy")

    account = await ledger_service.load_paper_account(
        db_session, user_id=user.id, connection=paper, country="argentina"
    )
    assert {position.symbol for position in account.positions} == {"GGAL", "YPFD"}
    assert account.cash == money("100.00")


@pytest.mark.parametrize("cash", ["", "abc", "0", "-5"])
async def test_a_bad_starting_cash_is_refused(db_session, make_user, fake_iol, cash) -> None:
    user = await make_user("alice")
    source = await _source_with_portfolio(db_session, user, fake_iol)

    with pytest.raises(ledger_service.LedgerError):
        await _paper(db_session, user, source, starting="cash", cash=cash)


async def test_copying_needs_a_saved_portfolio_on_the_source(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    source = await make_connection(db_session, user, fake_iol)  # nothing read yet

    with pytest.raises(ledger_service.LedgerError, match="saved portfolio"):
        await _paper(db_session, user, source, starting="copy")


async def test_reset_returns_the_account_to_its_starting_point(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    source = await _source_with_portfolio(db_session, user, fake_iol)
    paper = await _paper(db_session, user, source, starting="cash", cash="1000")
    spent = PaperLedger(country="argentina", currency="Peso_Argentino", cash=money("1.00"))
    await ledger_service.save_paper_account(
        db_session, user_id=user.id, connection=paper, ledger=spent
    )

    await ledger_service.reset_paper_account(db_session, user_id=user.id, connection=paper)

    account = await ledger_service.load_paper_account(
        db_session, user_id=user.id, connection=paper, country="argentina"
    )
    assert account.cash == money("1000.00")


async def test_a_paper_account_needs_a_real_price_source(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    source = await _source_with_portfolio(db_session, user, fake_iol)
    paper = await _paper(db_session, user, source, starting="cash", cash="1000", label="first")

    with pytest.raises(connections_service.ConnectionValidationError, match="another one"):
        await connections_service.create_broker_connection(
            db_session,
            user_id=user.id,
            broker="paper",
            label="second",
            fields={},
            price_source_connection_id=paper.id,
        )


async def test_a_price_source_must_belong_to_the_same_user(db_session, make_user, fake_iol) -> None:
    alice = await make_user("alice")
    mallory = await make_user("mallory")
    others = await make_connection(db_session, mallory, fake_iol)

    with pytest.raises(connections_service.ConnectionNotFoundError):
        await connections_service.create_broker_connection(
            db_session,
            user_id=alice.id,
            broker="paper",
            label="mine",
            fields={},
            price_source_connection_id=others.id,
        )


# --- The reader ------------------------------------------------------------------


async def test_the_paper_reader_serves_the_account_in_the_shared_format(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    source = await _source_with_portfolio(db_session, user, fake_iol)
    paper = await _paper(db_session, user, source, starting="copy")

    reader = await broker_service.open_reader(paper, db=db_session)
    try:
        portfolio = await reader.get_portfolio("argentina")
        balances = await reader.get_balances()
    finally:
        await reader.aclose()

    assert isinstance(reader, BrokerReader) and isinstance(reader, PaperBroker)
    assert portfolio.cash == 100.0
    assert {item.instrument.symbol for item in portfolio.positions} == {"GGAL", "YPFD"}
    assert balances[0].available == Decimal("100.00")


async def test_paper_prices_come_from_the_price_source(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    source = await _source_with_portfolio(db_session, user, fake_iol)
    paper = await _paper(db_session, user, source, starting="copy")

    reader = await broker_service.open_reader(paper, db=db_session)
    try:
        bars = await reader.get_history(symbol="GGAL", market="BCBA", days=30)
    finally:
        await reader.aclose()

    assert len(bars) == 45 and all(isinstance(bar, PriceBar) for bar in bars)


async def test_an_account_that_was_never_started_says_so() -> None:
    async def nothing(_country):
        return None

    reader = PaperBroker(country="argentina", load_account=nothing, price_source=None)

    with pytest.raises(BrokerError, match="not been started"):
        await reader.get_portfolio("argentina")


async def test_without_a_price_source_history_is_unavailable_and_orders_do_not_exist() -> None:
    async def nothing(_country):
        return None

    reader = PaperBroker(country="argentina", load_account=nothing, price_source=None)

    with pytest.raises(BrokerUnavailableError, match="take prices"):
        await reader.get_history(symbol="GGAL", market="BCBA", days=5)
    with pytest.raises(BrokerError, match="no broker operation"):
        await reader.get_operation("1")


async def test_live_trading_is_never_available_for_a_paper_account(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    source = await _source_with_portfolio(db_session, user, fake_iol)
    paper = await _paper(db_session, user, source, starting="copy")

    with pytest.raises(BrokerUnavailableError, match="not available"):
        await broker_service.trading_session(paper)


# --- A whole run on a paper account -------------------------------------------------


async def test_a_paper_run_never_reaches_a_broker_and_only_executed_trades_change_it(
    db_session, make_user, fake_iol, fake_advisor
) -> None:
    user = await make_user("alice")
    source = await _source_with_portfolio(db_session, user, fake_iol)
    paper = await _paper(db_session, user, source, starting="copy")
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.90},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )

    state = await trading_flow.start_run(
        db_session, user_id=user.id, connection=paper, country="argentina"
    )

    assert state["status"] == trading_flow.STATUS_AWAITING_APPROVAL
    record = await analysis_service.get_proposal(
        db_session, user_id=user.id, proposal_id=state["proposal_id"]
    )
    proposal = Proposal.model_validate(record.payload)
    assert proposal.execution_mode == "PAPER"
    # Planning is a preview: the account itself has not moved.
    account = await ledger_service.load_paper_account(
        db_session, user_id=user.id, connection=paper, country="argentina"
    )
    assert account.position_for("GGAL") is not None

    await execution_service.execute_single_order(
        db_session,
        user_id=user.id,
        connection=paper,
        proposal_record=record,
        symbol="GGAL",
        side="SELL",
        units=Decimal("100"),
    )

    after = await ledger_service.load_paper_account(
        db_session, user_id=user.id, connection=paper, country="argentina"
    )
    assert after.position_for("GGAL") is None
    assert after.cash > account.cash
    assert fake_iol.trade_posts == []  # no order reached any broker


async def test_a_paper_purchase_needs_no_broker_cash_and_reaches_no_broker(
    db_session, make_user, fake_iol, fake_advisor
) -> None:
    user = await make_user("alice")
    source = await _source_with_portfolio(db_session, user, fake_iol)
    paper = await _paper(db_session, user, source, starting="copy")
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.65, "VIST": 0.25},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )
    state = await trading_flow.start_run(
        db_session, user_id=user.id, connection=paper, country="argentina"
    )
    record = await analysis_service.get_proposal(
        db_session, user_id=user.id, proposal_id=state["proposal_id"]
    )
    await execution_service.execute_single_order(
        db_session,
        user_id=user.id,
        connection=paper,
        proposal_record=record,
        symbol="GGAL",
        side="SELL",
        units=Decimal("100"),
    )
    asked_before = list(fake_iol.authenticated_paths).count("/api/v2/estadocuenta")

    await execution_service.execute_single_order(
        db_session,
        user_id=user.id,
        connection=paper,
        proposal_record=record,
        symbol="VIST",
        side="BUY",
        units=Decimal("1"),
    )

    account = await ledger_service.load_paper_account(
        db_session, user_id=user.id, connection=paper, country="argentina"
    )
    assert account.position_for("VIST").quantity == Decimal("1")
    assert fake_iol.trade_posts == []
    assert list(fake_iol.authenticated_paths).count("/api/v2/estadocuenta") == asked_before


async def test_a_paper_run_does_not_ask_the_broker_for_an_account_status(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    source = await _source_with_portfolio(db_session, user, fake_iol)
    paper = await _paper(db_session, user, source, starting="copy")
    before = list(fake_iol.authenticated_paths).count("/api/v2/estadocuenta")

    state = await trading_flow.start_run(
        db_session, user_id=user.id, connection=paper, country="argentina"
    )

    assert state["status"] == trading_flow.STATUS_AWAITING_APPROVAL
    assert list(fake_iol.authenticated_paths).count("/api/v2/estadocuenta") == before


# --- The pages ---------------------------------------------------------------------


async def test_a_paper_account_is_created_through_the_form_and_shows_its_cash(
    client, db_session, make_user, password, fake_iol
) -> None:
    user = await make_user("alice", password)
    source = await _source_with_portfolio(db_session, user, fake_iol)
    await login(client, "alice", password)
    page = await client.get("/connections/new?broker=paper")
    assert "Take prices from" in page.text

    response = await client.post(
        "/connections",
        data={
            "broker": "paper",
            "label": "Practice",
            "country": "argentina",
            "price_source": str(source.id),
            "starting": "cash",
            "initial_cash": "5000",
            "csrf_token": extract_csrf(page.text),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    detail = await client.get(response.headers["location"])
    assert "Paper account" in detail.text and "5.000,00" in detail.text
    assert "Reset to the starting point" in detail.text


async def test_a_failed_paper_start_leaves_no_half_made_connection(
    client, db_session, make_user, password, fake_iol
) -> None:
    user = await make_user("alice", password)
    source = await _source_with_portfolio(db_session, user, fake_iol)
    await login(client, "alice", password)
    page = await client.get("/connections/new?broker=paper")

    response = await client.post(
        "/connections",
        data={
            "broker": "paper",
            "label": "Broken",
            "country": "argentina",
            "price_source": str(source.id),
            "starting": "cash",
            "initial_cash": "not a number",
            "csrf_token": extract_csrf(page.text),
        },
    )

    assert response.status_code == 400
    assert "must be a number" in response.text
    labels = [
        c.label for c in await connections_service.list_connections(db_session, user_id=user.id)
    ]
    assert labels == ["My IOL account"]


async def test_a_paper_account_without_a_price_source_is_refused(
    client, db_session, make_user, password
) -> None:
    await make_user("alice", password)
    await login(client, "alice", password)
    page = await client.get("/connections/new?broker=paper")

    response = await client.post(
        "/connections",
        data={
            "broker": "paper",
            "label": "Nowhere",
            "starting": "cash",
            "initial_cash": "100",
            "csrf_token": extract_csrf(page.text),
        },
    )

    assert response.status_code == 400
    assert "Pick a connection" in response.text


async def test_reset_works_only_for_the_owner_and_only_for_a_paper_account(
    client, db_session, make_user, password, fake_iol
) -> None:
    alice = await make_user("alice", password)
    mallory = await make_user("mallory", password)
    source = await _source_with_portfolio(db_session, alice, fake_iol)
    paper = await _paper(db_session, alice, source, starting="cash", cash="1000")

    await login(client, "mallory", password)
    token = extract_csrf((await client.get("/dashboard")).text)
    stolen = await client.post(f"/connections/{paper.id}/reset", data={"csrf_token": token})
    assert stolen.status_code == 404
    assert mallory.id != alice.id

    client.cookies.clear()  # a different person signs in
    await login(client, "alice", password)
    token = extract_csrf((await client.get("/dashboard")).text)
    not_paper = await client.post(f"/connections/{source.id}/reset", data={"csrf_token": token})
    assert not_paper.status_code == 400
    mine = await client.post(
        f"/connections/{paper.id}/reset", data={"csrf_token": token}, follow_redirects=False
    )
    assert mine.status_code == 303 and "reset=ok" in mine.headers["location"]
