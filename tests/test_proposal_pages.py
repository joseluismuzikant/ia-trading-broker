"""Browser-flow tests for the proposal review, approval, and history pages."""

from __future__ import annotations

import re

from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.day5_helpers import make_connection, prepare_tradeable_account
from tests.helpers import extract_csrf, login

PROPOSAL_ID_RE = re.compile(r"proposal_id=(\d+)")


def _enable_live(monkeypatch) -> None:
    """Configure the IOL trading terms live execution needs."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "iol_live_plazo", "t2")
    monkeypatch.setattr(settings, "iol_live_validity_days", 1)


async def _setup(client, db_session, make_user, password, fake_iol, username="alice"):
    """Create a ready account, log in, and return (user, connection)."""
    user = await make_user(username, password)
    connection = await make_connection(db_session, user, fake_iol)
    await prepare_tradeable_account(db_session, user=user, connection=connection)
    await login(client, username, password)
    return user, connection


async def _run_analysis(client, connection) -> int:
    """Run the workflow through the page and return the proposal id."""
    page = await client.get(f"/analysis?connection_id={connection.id}&country=argentina")
    response = await client.post(
        "/analysis/run",
        data={
            "csrf_token": extract_csrf(page.text),
            "connection_id": connection.id,
            "country": "argentina",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "analysis=ok" in location
    match = PROPOSAL_ID_RE.search(location)
    assert match is not None, location
    return int(match.group(1))


# --- Access control -------------------------------------------------------


async def test_review_page_requires_login(client) -> None:
    response = await client.get("/proposals/1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_history_page_requires_login(client) -> None:
    response = await client.get("/history", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_approve_requires_login(client) -> None:
    response = await client.post("/proposals/1/approve", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- The review page ------------------------------------------------------


async def test_a_paused_run_shows_the_review_page(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)

    response = await client.get(f"/proposals/{proposal_id}")

    assert response.status_code == 200
    assert "Approve plan" in response.text
    assert "HUMAN_IN_THE_LOOP" in response.text
    assert "PENDING" in response.text
    assert "No order has been saved" in response.text
    # The review shows the model allocation and the before/after preview.
    assert "Model target allocation" in response.text
    assert "Current portfolio vs simulated paper portfolio" in response.text
    # Each trade waits for the decision, and offers its own Execute button.
    assert "approve the plan first" in response.text


async def test_the_analysis_page_links_to_the_review(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)

    page = await client.get(
        f"/analysis?connection_id={connection.id}&country=argentina&proposal_id={proposal_id}"
    )

    assert page.status_code == 200
    assert f"/proposals/{proposal_id}" in page.text
    assert "Approving or rejecting happens on the proposal page" in page.text


# --- Approving ------------------------------------------------------------


async def test_approving_sends_nothing_and_trade_buttons_wait_for_the_decision(
    client, db_session, make_user, password, fake_iol, fake_advisor, monkeypatch
) -> None:
    from app.infrastructure.simulation import advice_from_targets

    _enable_live(monkeypatch)
    user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )
    proposal_id = await _run_analysis(client, connection)
    review = await client.get(f"/proposals/{proposal_id}")
    assert "approve the plan first" in review.text

    response = await client.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": extract_csrf(review.text)},
        follow_redirects=True,
    )

    # Approval records the decision and sends nothing by itself.
    assert response.status_code == 200
    assert "Plan approved" in response.text
    assert fake_iol.trade_posts == []
    review = await client.get(f"/proposals/{proposal_id}")
    assert "approve the plan first" not in review.text
    assert 'name="quantity"' in review.text


async def test_a_trade_is_executed_at_the_amount_the_user_chooses(
    client, db_session, make_user, password, fake_iol, fake_advisor, monkeypatch
) -> None:
    from app.infrastructure.simulation import advice_from_targets

    _enable_live(monkeypatch)
    user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )
    proposal_id = await _run_analysis(client, connection)
    review = await client.get(f"/proposals/{proposal_id}")
    token = extract_csrf(review.text)
    await client.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": token},
        follow_redirects=True,
    )

    # The plan suggests selling all 100 GGAL; the user chooses one.
    response = await client.post(
        f"/proposals/{proposal_id}/execute",
        data={
            "csrf_token": token,
            "symbol": "GGAL",
            "side": "SELL",
            "quantity": "1",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert fake_iol.trade_posts != []
    path, body = fake_iol.trade_posts[0]
    assert path == "/api/v2/operar/Vender"
    assert body["cantidad"] == "1"


async def test_execute_without_trading_terms_sends_nothing(
    client, db_session, make_user, password, fake_iol, fake_advisor
) -> None:
    from app.infrastructure.simulation import advice_from_targets

    user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )
    proposal_id = await _run_analysis(client, connection)
    review = await client.get(f"/proposals/{proposal_id}")
    token = extract_csrf(review.text)
    await client.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": token},
        follow_redirects=True,
    )

    response = await client.post(
        f"/proposals/{proposal_id}/execute",
        data={"csrf_token": token, "symbol": "GGAL", "side": "SELL", "quantity": "1"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "IOL_LIVE_PLAZO is not configured" in response.text
    assert fake_iol.trade_posts == []


async def test_approving_twice_is_reported(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)
    review = await client.get(f"/proposals/{proposal_id}")
    token = extract_csrf(review.text)
    await client.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": token},
        follow_redirects=True,
    )

    response = await client.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": token},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "could not be saved" in response.text or "already" in response.text


async def test_rejecting_from_the_review_page(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)
    review = await client.get(f"/proposals/{proposal_id}")

    response = await client.post(
        f"/proposals/{proposal_id}/reject",
        data={"csrf_token": extract_csrf(review.text)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Rejected. Nothing was sent to the broker." in response.text
    assert "No order has been saved" in response.text


async def test_approving_without_a_csrf_token_is_rejected(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)

    response = await client.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": "forged"},
        follow_redirects=False,
    )

    assert response.status_code == 400


# --- History --------------------------------------------------------------


async def test_history_lists_the_order_after_a_live_submission(
    client, db_session, make_user, password, fake_iol, fake_advisor, monkeypatch
) -> None:
    from app.infrastructure.simulation import advice_from_targets

    _enable_live(monkeypatch)
    user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )
    proposal_id = await _run_analysis(client, connection)
    review = await client.get(f"/proposals/{proposal_id}")
    token = extract_csrf(review.text)
    await client.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    await client.post(
        f"/proposals/{proposal_id}/execute",
        data={"csrf_token": token, "symbol": "GGAL", "side": "SELL", "quantity": "1"},
        follow_redirects=True,
    )

    response = await client.get(f"/history?connection_id={connection.id}")

    assert response.status_code == 200
    assert "Trading history" in response.text
    assert "order.accepted" in response.text
    assert f"/proposals/{proposal_id}" in response.text


# --- Tenant isolation -----------------------------------------------------


async def test_another_user_cannot_open_the_review(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)

    await make_user("bob", password)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as bob:
        await login(bob, "bob", password)
        response = await bob.get(f"/proposals/{proposal_id}", follow_redirects=False)

    assert response.status_code == 404


# --- PDF export and the identifier wording --------------------------------


async def test_the_review_page_names_the_identifier_and_offers_an_export(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)

    response = await client.get(f"/proposals/{proposal_id}")

    assert response.status_code == 200
    # The number of the plan is named as an identifier.
    assert f"Proposal ID {proposal_id}" in response.text
    assert "Export PDF" in response.text
    assert f'href="/proposals/{proposal_id}/export"' in response.text


async def test_the_pdf_export_renders_the_whole_analysis(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)

    response = await client.get(f"/proposals/{proposal_id}/export")

    assert response.status_code == 200
    assert f"Proposal ID {proposal_id}" in response.text
    assert "Key figures" in response.text
    assert "Plan risk checks" in response.text
    assert "Model target allocation" in response.text
    assert "Current portfolio vs simulated paper portfolio" in response.text
    assert "Plan" in response.text
    # The document prints itself, and it prints without the site chrome.
    assert "window.print()" in response.text
    assert "Save as PDF" in response.text
    assert 'class="nav"' not in response.text
    # Money and timestamps are formatted for a local reader.
    assert " ARS" in response.text
    assert "UTC-3" in response.text
    assert "99.895" not in response.text or "," in response.text


async def test_the_pdf_export_is_private_to_the_proposal_owner(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)

    await make_user("mallory", password)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as mallory:
        await login(mallory, "mallory", password)
        response = await mallory.get(f"/proposals/{proposal_id}/export")

    assert response.status_code == 404


# --- The Proposals list page ----------------------------------------------


async def test_the_proposals_page_lists_your_own_proposals_newest_first(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    first = await _run_analysis(client, connection)
    second = await _run_analysis(client, connection)

    response = await client.get("/proposals")

    assert response.status_code == 200
    assert "Proposals" in response.text
    assert f"Proposal #{second}" in response.text
    assert f"Proposal #{first}" in response.text
    # Newest first.
    assert response.text.index(f"Proposal #{second}") < response.text.index(f"Proposal #{first}")
    assert "PENDING" in response.text
    assert "Export PDF" in response.text


async def test_the_proposals_page_shows_no_other_account_proposals(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    mine = await _run_analysis(client, connection)

    await make_user("bob", password)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as bob:
        await login(bob, "bob", password)
        response = await bob.get("/proposals")

        assert response.status_code == 200
        assert f"Proposal #{mine}" not in response.text
        assert "No proposals yet" in response.text


async def test_the_proposals_page_requires_login(client) -> None:
    response = await client.get("/proposals", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- The proposal detail page ---------------------------------------------


async def test_the_proposal_page_shows_trades_cash_and_the_consequence(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)

    response = await client.get(f"/proposals/{proposal_id}")

    assert response.status_code == 200
    assert f"Proposal #{proposal_id}" in response.text
    assert "Current vs target allocation" in response.text
    assert "Trades to execute" in response.text
    assert "Cash summary" in response.text
    assert "Estimated cash after rebalance" in response.text
    assert "AI summary" in response.text
    assert "Main risks" in response.text
    assert "Human review" in response.text
    assert "Technical details" in response.text


async def test_the_status_badges_are_human_readable(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)

    response = await client.get(f"/proposals/{proposal_id}")
    assert "PENDING" in response.text
    assert "pending_review" not in response.text

    await client.post(
        f"/proposals/{proposal_id}/reject",
        data={"csrf_token": extract_csrf((await client.get(f"/proposals/{proposal_id}")).text)},
        follow_redirects=True,
    )
    response = await client.get(f"/proposals/{proposal_id}")
    assert "REJECTED" in response.text


# --- Safe deletion ---------------------------------------------------------


async def test_deleting_a_pending_proposal_removes_it_and_keeps_an_audit_line(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)

    listing = await client.get("/proposals")
    response = await client.post(
        f"/proposals/{proposal_id}/delete",
        data={"csrf_token": extract_csrf(listing.text)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Proposal deleted" in response.text
    assert f"Proposal #{proposal_id}" not in response.text

    from app.services import analysis as analysis_service

    assert (
        await analysis_service.get_proposal(db_session, user_id=_user.id, proposal_id=proposal_id)
        is None
    )
    events = await client.get(f"/history?connection_id={connection.id}")
    assert "proposal.deleted" in events.text


async def test_deletion_is_refused_while_orders_wait_at_the_broker(
    client, db_session, make_user, password, fake_iol
) -> None:
    from decimal import Decimal

    from app.domain.money import money
    from app.domain.trading import Order
    from app.services import analysis as analysis_service
    from app.services import orders as orders_service

    user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)
    await analysis_service.get_proposal(db_session, user_id=user.id, proposal_id=proposal_id)
    await orders_service.prepare_order(
        db_session,
        user_id=user.id,
        connection_id=connection.id,
        proposal_id=proposal_id,
        country="argentina",
        order=Order(
            symbol="GGAL",
            side="SELL",
            quantity=Decimal("1"),
            limit_price=money("60.00"),
            notional=money("60.00"),
            market="BCBA",
            status="accepted",
            execution_mode="LIVE",
        ),
    )

    listing = await client.get("/proposals")
    response = await client.post(
        f"/proposals/{proposal_id}/delete",
        data={"csrf_token": extract_csrf(listing.text)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "not settled yet" in response.text
    assert "Delete unavailable" in response.text
    assert (
        await analysis_service.get_proposal(db_session, user_id=user.id, proposal_id=proposal_id)
        is not None
    )


async def test_deletion_is_owner_only_and_csrf_protected(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)

    response = await client.post(
        f"/proposals/{proposal_id}/delete",
        data={"csrf_token": "forged"},
        follow_redirects=False,
    )
    assert response.status_code == 400

    await make_user("bob", password)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as bob:
        await login(bob, "bob", password)
        response = await bob.post(
            f"/proposals/{proposal_id}/delete",
            data={"csrf_token": "forged"},
            follow_redirects=False,
        )
        assert response.status_code in (400, 404)


# --- The PDF export --------------------------------------------------------


async def test_the_pdf_export_follows_the_new_hierarchy(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)

    response = await client.get(f"/proposals/{proposal_id}/export")

    assert response.status_code == 200
    assert f"Proposal #{proposal_id}" in response.text
    for heading in (
        "Key figures",
        "Current vs target allocation",
        "Trades to execute",
        "Cash summary",
        "AI summary",
        "Main risks",
        "Human review",
        "Technical details",
    ):
        assert heading in response.text, heading
    # Still saved data only, rendered for print.
    assert "window.print()" in response.text
    assert 'class="nav"' not in response.text


async def test_the_proposal_page_lists_a_not_executable_trade(
    client, db_session, make_user, password, fake_iol
) -> None:
    """A trade the model wanted but Python cannot size is shown, with a reason."""
    from app.domain.money import money
    from app.domain.trading import (
        IndicatorEvidence,
        PortfolioAdviceView,
        Proposal,
        Recommendation,
    )
    from app.services import analysis as analysis_service

    user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    advice = PortfolioAdviceView(
        summary="test",
        target_cash_weight=0.85,
        current_cash_weight=1.0,
        model_label="test",
        model_provider="test",
        model_name="test",
        analysis_version="test-1",
        decisions=[],
    )
    blocked = Recommendation(
        symbol="MCD",
        name="McDonald's Corporation",
        action="BUY",
        quantity=0,
        limit_price=money("15530.00"),
        notional=money("0"),
        target_weight=0.10,
        market="BCBA",
        executable=False,
        reason="one unit costs 15530.00 but the position cap leaves less room",
        required_quantity=0,
        min_quantity=1,
        unit_cost=money("15530.00"),
        rationale="add consumer exposure",
        evidence=IndicatorEvidence(),
    )
    proposal = Proposal(
        country="argentina",
        execution_mode="LIVE",
        portfolio_value=money("100000.00"),
        cash=money("100000.00"),
        advice=advice,
        recommendations=[blocked],
        summary="test",
    )
    record = await analysis_service.save_proposal(
        db_session, user_id=user.id, connection_id=connection.id, proposal=proposal
    )

    response = await client.get(f"/proposals/{record.id}")

    assert response.status_code == 200
    assert "Not executable" in response.text
    assert "NOT EXECUTABLE" in response.text
    assert "the position cap leaves less room" in response.text
    assert "MCD" in response.text
    # One unit is 15.53% of the portfolio, above the 15% position limit.
    assert "One unit costs" in response.text
    assert "of the portfolio" in response.text
    assert "above the" in response.text
    assert "What you can do." in response.text


# --- Resume execution -----------------------------------------------------


async def test_resume_continues_to_the_purchases_phase(
    client, db_session, make_user, password, fake_iol, fake_advisor, monkeypatch
) -> None:
    from app.infrastructure.simulation import advice_from_targets

    _enable_live(monkeypatch)
    user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )
    proposal_id = await _run_analysis(client, connection)

    review = await client.get(f"/proposals/{proposal_id}")
    token = extract_csrf(review.text)
    await client.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert fake_iol.trade_posts == []

    # First resume drives the sales phase.
    review = await client.get(f"/proposals/{proposal_id}")
    await client.post(
        f"/proposals/{proposal_id}/resume",
        data={"csrf_token": extract_csrf(review.text)},
        follow_redirects=True,
    )
    assert [path for path, _ in fake_iol.trade_posts] == [
        "/api/v2/operar/Vender",
        "/api/v2/operar/Vender",
    ]

    # Give the broker cash so the purchase is fundable, then resume again.
    fake_iol.account_status["cuentas"][0]["disponible"] = 100_000.0
    review = await client.get(f"/proposals/{proposal_id}")
    response = await client.post(
        f"/proposals/{proposal_id}/resume",
        data={"csrf_token": extract_csrf(review.text)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "/api/v2/operar/Comprar" in [path for path, _ in fake_iol.trade_posts]
    # Nothing was sent twice.
    assert len(fake_iol.trade_posts) == 3


async def test_resume_is_safe_to_repeat(
    client, db_session, make_user, password, fake_iol, fake_advisor, monkeypatch
) -> None:
    from app.infrastructure.simulation import advice_from_targets

    _enable_live(monkeypatch)
    user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )
    proposal_id = await _run_analysis(client, connection)
    review = await client.get(f"/proposals/{proposal_id}")
    token = extract_csrf(review.text)
    await client.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    fake_iol.account_status["cuentas"][0]["disponible"] = 100_000.0

    # Drive both phases to completion.
    for _ in range(2):
        review = await client.get(f"/proposals/{proposal_id}")
        await client.post(
            f"/proposals/{proposal_id}/resume",
            data={"csrf_token": extract_csrf(review.text)},
            follow_redirects=True,
        )
    sent = len(fake_iol.trade_posts)
    assert sent == 3

    review = await client.get(f"/proposals/{proposal_id}")
    await client.post(
        f"/proposals/{proposal_id}/resume",
        data={"csrf_token": extract_csrf(review.text)},
        follow_redirects=True,
    )

    assert len(fake_iol.trade_posts) == sent


async def test_resume_before_a_decision_is_refused(
    client, db_session, make_user, password, fake_iol
) -> None:
    user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)
    review = await client.get(f"/proposals/{proposal_id}")

    response = await client.post(
        f"/proposals/{proposal_id}/resume",
        data={"csrf_token": extract_csrf(review.text)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "not approved" in response.text
    assert "nothing can be executed" in response.text


async def test_resume_requires_login_csrf_and_ownership(
    client, db_session, make_user, password, fake_iol
) -> None:
    user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    proposal_id = await _run_analysis(client, connection)

    response = await client.post(
        f"/proposals/{proposal_id}/resume",
        data={"csrf_token": "forged"},
        follow_redirects=False,
    )
    assert response.status_code == 400

    await make_user("bob", password)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as bob:
        await login(bob, "bob", password)
        response = await bob.post(
            f"/proposals/{proposal_id}/resume",
            data={"csrf_token": "forged"},
            follow_redirects=False,
        )
        assert response.status_code in (400, 404)


async def test_resume_without_login_redirects_to_login(client) -> None:
    response = await client.post("/proposals/1/resume", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_a_failed_trade_can_be_retried_one_by_one(
    client, db_session, make_user, password, fake_iol, fake_advisor, monkeypatch
) -> None:
    """A proven failure may be retried; an unconfirmed order never is."""
    from app.infrastructure.simulation import advice_from_targets

    _enable_live(monkeypatch)
    user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )
    proposal_id = await _run_analysis(client, connection)
    review = await client.get(f"/proposals/{proposal_id}")
    token = extract_csrf(review.text)
    await client.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": token},
        follow_redirects=True,
    )

    # First attempt is refused by the broker.
    fake_iol.trade_responses.append(
        {"status": 202, "json": {"ok": False, "messages": [{"description": "refused"}]}}
    )
    response = await client.post(
        f"/proposals/{proposal_id}/execute",
        data={"csrf_token": token, "symbol": "GGAL", "side": "SELL", "quantity": "1"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert len(fake_iol.trade_posts) == 1

    # A refused order is a proven failure: the same trade may be retried.
    review = await client.get(f"/proposals/{proposal_id}")
    assert "Retry" in review.text
    response = await client.post(
        f"/proposals/{proposal_id}/execute",
        data={"csrf_token": token, "symbol": "GGAL", "side": "SELL", "quantity": "1"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert len(fake_iol.trade_posts) == 2

    # The second attempt is at the broker and unconfirmed: no third send.
    response = await client.post(
        f"/proposals/{proposal_id}/execute",
        data={"csrf_token": token, "symbol": "GGAL", "side": "SELL", "quantity": "1"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "already sent" in response.text or "cannot be sent again" in response.text
    assert len(fake_iol.trade_posts) == 2


async def test_a_stale_approval_sends_nothing(
    client, db_session, make_user, password, fake_iol, fake_advisor, monkeypatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.infrastructure.simulation import advice_from_targets
    from app.models import Approval

    _enable_live(monkeypatch)
    user, connection = await _setup(client, db_session, make_user, password, fake_iol)
    fake_advisor.payload = advice_from_targets(
        {"GGAL": 0.0, "YPFD": 0.55, "VIST": 0.35},
        cash=0.10,
        current={"GGAL": 0.3336, "YPFD": 0.6609},
    )
    proposal_id = await _run_analysis(client, connection)
    review = await client.get(f"/proposals/{proposal_id}")
    token = extract_csrf(review.text)
    await client.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    # The decision was made more than a day ago.
    approval = (
        await db_session.execute(select(Approval).where(Approval.proposal_id == proposal_id))
    ).scalar_one()
    approval.decided_at = datetime.now(UTC) - timedelta(hours=25)
    await db_session.commit()

    response = await client.post(
        f"/proposals/{proposal_id}/execute",
        data={"csrf_token": token, "symbol": "GGAL", "side": "SELL", "quantity": "1"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "older than 24 hours" in response.text
    assert fake_iol.trade_posts == []
