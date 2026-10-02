"""Browser-flow tests for the country-portfolio page."""

from __future__ import annotations

from tests.helpers import create_connection, csrf_from_dashboard, extract_csrf, login


async def logged_in_user(client, make_user, password: str, username: str = "alice"):
    """Create a user, log in, and return the user row."""
    user = await make_user(username, password)
    await login(client, username, password)
    return user


# --- Access control -------------------------------------------------------


async def test_portfolio_page_requires_login(client) -> None:
    response = await client.get("/portfolio", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_portfolio_refresh_requires_login(client) -> None:
    response = await client.post("/portfolio/refresh", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- Empty states ---------------------------------------------------------


async def test_portfolio_page_without_a_connection(
    client, make_user, password: str
) -> None:
    await logged_in_user(client, make_user, password)

    response = await client.get("/portfolio")

    assert response.status_code == 200
    assert "No broker connection yet" in response.text


async def test_portfolio_page_before_the_first_refresh(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    response = await client.get("/portfolio")

    assert response.status_code == 200
    assert "No snapshot saved yet" in response.text
    assert "Nothing saved yet for this country" in response.text


# --- A successful refresh -------------------------------------------------


async def test_refreshing_the_portfolio_shows_positions_and_prices(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    page = await client.get("/portfolio")
    refreshed = await client.post(
        "/portfolio/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )

    assert refreshed.status_code == 200
    assert "Refreshed from IOL" in refreshed.text
    assert "GGAL" in refreshed.text
    assert "Grupo Financiero Galicia" in refreshed.text
    assert "YPFD" in refreshed.text
    # Total quantity, the free quantity, and the last price.
    assert "100,00" in refreshed.text
    assert "75,00" in refreshed.text
    assert "1.200,00" in refreshed.text
    assert "source:" in refreshed.text
    assert "fetched" in refreshed.text


async def test_the_country_selector_lists_both_countries(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    response = await client.get("/portfolio")

    assert "Argentina" in response.text
    assert "Estados Unidos" in response.text


async def test_the_page_shows_how_many_iol_calls_were_made(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)
    page = await client.get("/portfolio")
    refreshed = await client.post(
        "/portfolio/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )

    assert "IOL API calls this month" in refreshed.text
    # A login plus one read: refreshing a page is intentionally cheap.
    assert fake_iol.token_grants == ["password"]
    assert fake_iol.authenticated_paths == ["/api/v2/portafolio/argentina"]


async def test_cash_and_total_use_the_saved_account_status(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    status_page = await client.get("/account-status")
    await client.post(
        "/account-status/refresh",
        data={"csrf_token": extract_csrf(status_page.text)},
        follow_redirects=True,
    )
    page = await client.get("/portfolio")
    refreshed = await client.post(
        "/portfolio/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )

    # Cash 100.00 from the account status plus 18055.50 of positions.
    assert "18.155,50" in refreshed.text


# --- Countries are kept apart --------------------------------------------


async def test_switching_country_uses_a_separate_snapshot(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client, country="argentina")
    page = await client.get("/portfolio")
    await client.post(
        "/portfolio/refresh",
        data={"csrf_token": extract_csrf(page.text), "country": "argentina"},
        follow_redirects=True,
    )

    # The other country has nothing saved yet.
    other = await client.get("/portfolio?country=estados_unidos")
    assert "Nothing saved yet for this country" in other.text

    # Argentina is untouched.
    argentina = await client.get("/portfolio?country=argentina")
    assert "GGAL" in argentina.text


async def test_an_unsupported_country_is_refused(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    response = await client.get("/portfolio?country=atlantis", follow_redirects=False)

    assert response.status_code == 400


# --- Failure and staleness ------------------------------------------------


async def test_a_failed_refresh_keeps_the_last_snapshot_and_marks_it_stale(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)
    page = await client.get("/portfolio")
    await client.post(
        "/portfolio/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )

    fake_iol.portfolio_error_status = 503
    page = await client.get("/portfolio")
    stale = await client.post(
        "/portfolio/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )

    assert stale.status_code == 200
    assert "stale" in stale.text
    # The last good positions are still on screen.
    assert "GGAL" in stale.text


async def test_a_failed_refresh_with_no_snapshot_says_so(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)
    fake_iol.portfolio_error_status = 500

    page = await client.get("/portfolio")
    response = await client.post(
        "/portfolio/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "no saved data yet" in response.text


async def test_a_refresh_without_a_csrf_token_is_rejected(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    response = await client.post(
        "/portfolio/refresh", data={"csrf_token": "forged"}, follow_redirects=False
    )

    assert response.status_code == 400
    assert fake_iol.token_grants == []


# --- Tenant isolation -----------------------------------------------------


async def test_another_user_cannot_open_the_portfolio(
    client, make_user, password: str
) -> None:
    await logged_in_user(client, make_user, password, username="alice")
    created = await create_connection(client, label="Alice account")
    connection_id = created.headers["location"].rsplit("/", 1)[1]

    from httpx import ASGITransport, AsyncClient

    from app.main import app

    await make_user("bob", password)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as bob:
        await login(bob, "bob", password)

        response = await bob.get(
            f"/portfolio?connection_id={connection_id}", follow_redirects=False
        )

    assert response.status_code == 404


async def test_another_user_cannot_refresh_the_portfolio(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password, username="alice")
    created = await create_connection(client, label="Alice account")
    connection_id = created.headers["location"].rsplit("/", 1)[1]

    from httpx import ASGITransport, AsyncClient

    from app.main import app

    await make_user("bob", password)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as bob:
        await login(bob, "bob", password)

        response = await bob.post(
            "/portfolio/refresh",
            data={
                "csrf_token": await csrf_from_dashboard(bob),
                "connection_id": connection_id,
            },
            follow_redirects=False,
        )

    assert response.status_code == 404
    assert fake_iol.token_grants == []
