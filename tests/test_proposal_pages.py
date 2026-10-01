"""Browser-flow tests for the proposal review, approval, and history pages."""

from __future__ import annotations

import re

from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.day5_helpers import make_connection, prepare_tradeable_account
from tests.helpers import extract_csrf, login

PROPOSAL_ID_RE = re.compile(r"proposal_id=(\d+)")


async def _setup(client, db_session, make_user, password, fake_iol, username="alice"):
    """Create a ready account, log in, and return (user, connection)."""
    user = await make_user(username, password)
    connection = await make_connection(db_session, user, fake_iol)
    await prepare_tradeable_account(db_session, user=user, connection=connection)
    await login(client, username, password)
    return user, connection


async def _run_analysis(client, connection) -> int:
    """Run the workflow through the page and return the proposal id."""
    page = await client.get(
        f"/analysis?connection_id={connection.id}&country=argentina"
    )
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
    _user, connection = await _setup(
        client, db_session, make_user, password, fake_iol
    )
    proposal_id = await _run_analysis(client, connection)

    response = await client.get(f"/proposals/{proposal_id}")

    assert response.status_code == 200
    assert "Approve and submit" in response.text
    assert "HUMAN_IN_THE_LOOP" in response.text
    assert "pending" in response.text
    assert "No order has been saved" in response.text
    # The review page shows the whole funnel, not just the orders.
    assert "Analysis funnel" in response.text
    assert "Market scanner" in response.text
    assert "Finalists" in response.text


async def test_the_analysis_page_links_to_the_review(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(
        client, db_session, make_user, password, fake_iol
    )
    proposal_id = await _run_analysis(client, connection)

    page = await client.get(
        f"/analysis?connection_id={connection.id}&country=argentina"
        f"&proposal_id={proposal_id}"
    )

    assert page.status_code == 200
    assert f"/proposals/{proposal_id}" in page.text
    assert "Approving re-checks prices and cash" in page.text


# --- Approving ------------------------------------------------------------


async def test_approving_submits_and_fills(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(
        client, db_session, make_user, password, fake_iol
    )
    proposal_id = await _run_analysis(client, connection)
    review = await client.get(f"/proposals/{proposal_id}")

    response = await client.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": extract_csrf(review.text)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Approved and sent to the paper executor" in response.text
    assert "filled" in response.text
    assert "ledger.updated" in response.text


async def test_approving_twice_is_reported(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(
        client, db_session, make_user, password, fake_iol
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
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": token},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "could not be saved" in response.text or "already" in response.text


async def test_rejecting_from_the_review_page(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(
        client, db_session, make_user, password, fake_iol
    )
    proposal_id = await _run_analysis(client, connection)
    review = await client.get(f"/proposals/{proposal_id}")

    response = await client.post(
        f"/proposals/{proposal_id}/reject",
        data={"csrf_token": extract_csrf(review.text)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Rejected. No order was sent." in response.text
    assert "No order has been saved" in response.text


async def test_approving_without_a_csrf_token_is_rejected(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(
        client, db_session, make_user, password, fake_iol
    )
    proposal_id = await _run_analysis(client, connection)

    response = await client.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": "forged"},
        follow_redirects=False,
    )

    assert response.status_code == 400


# --- History --------------------------------------------------------------


async def test_history_lists_the_order_after_a_fill(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(
        client, db_session, make_user, password, fake_iol
    )
    proposal_id = await _run_analysis(client, connection)
    review = await client.get(f"/proposals/{proposal_id}")
    await client.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": extract_csrf(review.text)},
        follow_redirects=True,
    )

    response = await client.get(f"/history?connection_id={connection.id}")

    assert response.status_code == 200
    assert "Trading history" in response.text
    assert "order.filled" in response.text
    assert f"/proposals/{proposal_id}" in response.text


# --- Tenant isolation -----------------------------------------------------


async def test_another_user_cannot_open_the_review(
    client, db_session, make_user, password, fake_iol
) -> None:
    _user, connection = await _setup(
        client, db_session, make_user, password, fake_iol
    )
    proposal_id = await _run_analysis(client, connection)

    await make_user("bob", password)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as bob:
        await login(bob, "bob", password)
        response = await bob.get(f"/proposals/{proposal_id}", follow_redirects=False)

    assert response.status_code == 404
