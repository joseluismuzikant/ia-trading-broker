"""Browser-flow tests for the connection, profile, and account-status pages."""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.crypto import decrypt_secret
from app.models import BrokerConnection
from tests.helpers import (
    create_connection,
    csrf_from_dashboard,
    extract_csrf,
    login,
)

IOL_PASSWORD = "iol-super-secret-password"


async def logged_in_user(client, make_user, password: str, username: str = "alice"):
    """Create a user, log in, and return the user row."""
    user = await make_user(username, password)
    await login(client, username, password)
    return user


# --- Access control -------------------------------------------------------


async def test_connections_page_requires_login(client) -> None:
    response = await client.get("/connections", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_profile_page_requires_login(client) -> None:
    response = await client.get("/profile", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_account_status_page_requires_login(client) -> None:
    response = await client.get("/account-status", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_refresh_requires_login(client) -> None:
    response = await client.post("/profile/refresh", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- Listing and creating -------------------------------------------------


async def test_empty_state_is_shown(client, make_user, password: str) -> None:
    await logged_in_user(client, make_user, password)

    response = await client.get("/connections")

    assert response.status_code == 200
    assert "No connection yet" in response.text


async def test_creating_a_connection_stores_it_encrypted(
    client, make_user, password: str, session_factory
) -> None:
    user = await logged_in_user(client, make_user, password)

    response = await create_connection(client, password=IOL_PASSWORD)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/connections/")

    async with session_factory() as session:
        stored = (await session.execute(select(BrokerConnection))).scalar_one()
        assert stored.user_id == user.id
        assert IOL_PASSWORD not in stored.password_encrypted
        assert decrypt_secret(stored.password_encrypted) == IOL_PASSWORD


async def test_the_password_is_never_rendered_back(
    client, make_user, password: str
) -> None:
    await logged_in_user(client, make_user, password)
    created = await create_connection(client, password=IOL_PASSWORD)

    detail = await client.get(created.headers["location"])
    listing = await client.get("/connections")
    form = await client.get("/connections/new")

    for response in (detail, listing, form):
        assert IOL_PASSWORD not in response.text


async def test_connection_detail_shows_safe_fields(client, make_user, password: str) -> None:
    await logged_in_user(client, make_user, password)
    created = await create_connection(client, label="Broker principal")

    detail = await client.get(created.headers["location"])

    assert detail.status_code == 200
    assert "Broker principal" in detail.text
    assert "iol-test-user" in detail.text
    assert "stored encrypted" in detail.text


async def test_a_duplicate_label_is_reported_on_the_form(
    client, make_user, password: str
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client, label="Duplicated")

    response = await create_connection(client, label="Duplicated")

    assert response.status_code == 400
    assert "already exists" in response.text
    assert IOL_PASSWORD not in response.text


async def test_empty_fields_are_reported(client, make_user, password: str) -> None:
    await logged_in_user(client, make_user, password)

    response = await create_connection(client, label="   ")

    assert response.status_code == 400
    assert "label is required" in response.text


async def test_an_unsupported_country_is_reported(client, make_user, password: str) -> None:
    await logged_in_user(client, make_user, password)

    response = await create_connection(client, country="atlantis")

    assert response.status_code == 400
    assert "country must be one of" in response.text


async def test_creating_without_a_csrf_token_is_rejected(
    client, make_user, password: str
) -> None:
    await logged_in_user(client, make_user, password)

    response = await client.post(
        "/connections",
        data={
            "label": "No CSRF",
            "username": "iol-test-user",
            "password": IOL_PASSWORD,
            "country": "argentina",
            "csrf_token": "forged",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400


async def test_deleting_a_connection_removes_it(client, make_user, password: str) -> None:
    await logged_in_user(client, make_user, password)
    created = await create_connection(client)
    detail = await client.get(created.headers["location"])
    connection_id = created.headers["location"].rsplit("/", 1)[1]

    response = await client.post(
        f"/connections/{connection_id}/delete",
        data={"csrf_token": extract_csrf(detail.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/connections"
    assert "No connection yet" in (await client.get("/connections")).text


# --- Tenant isolation -----------------------------------------------------


async def test_another_user_cannot_open_the_connection(
    client, make_user, password: str
) -> None:
    await logged_in_user(client, make_user, password, username="alice")
    created = await create_connection(client, label="Alice account")
    location = created.headers["location"]

    # A second browser session, logged in as bob.
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    await make_user("bob", password)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as bob:
        await login(bob, "bob", password)

        assert (await bob.get(location, follow_redirects=False)).status_code == 404
        assert (await bob.get("/connections")).text.count("Alice account") == 0
        # And bob cannot select alice's connection on the data pages.
        assert (
            await bob.get(f"/profile?connection_id={location.rsplit('/', 1)[1]}",
                          follow_redirects=False)
        ).status_code == 404


async def test_another_user_cannot_delete_the_connection(
    client, make_user, password: str, session_factory
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
            f"/connections/{connection_id}/delete",
            data={"csrf_token": await csrf_from_dashboard(bob)},
            follow_redirects=False,
        )

    assert response.status_code == 404
    async with session_factory() as session:
        remaining = (await session.execute(select(BrokerConnection))).scalars().all()
    assert len(remaining) == 1


# --- Connection test ------------------------------------------------------


async def test_testing_a_connection_reports_success(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    created = await create_connection(client)
    location = created.headers["location"]
    detail = await client.get(location)

    response = await client.post(
        f"{location}/test",
        data={"csrf_token": extract_csrf(detail.text)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "accepted the saved login" in response.text


async def test_testing_a_connection_reports_failure(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    created = await create_connection(client, password="the-wrong-password")
    location = created.headers["location"]
    detail = await client.get(location)

    response = await client.post(
        f"{location}/test",
        data={"csrf_token": extract_csrf(detail.text)},
        follow_redirects=True,
    )

    assert "connection test failed" in response.text.lower()
    assert "the-wrong-password" not in response.text


# --- Profile page ---------------------------------------------------------


async def test_profile_page_without_a_connection(client, make_user, password: str) -> None:
    await logged_in_user(client, make_user, password)

    response = await client.get("/profile")

    assert response.status_code == 200
    assert "No broker connection yet" in response.text


async def test_profile_page_before_the_first_refresh(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    response = await client.get("/profile")

    assert response.status_code == 200
    assert "No snapshot saved yet" in response.text


async def test_refreshing_the_profile_saves_and_shows_the_snapshot(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    page = await client.get("/profile")
    refreshed = await client.post(
        "/profile/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )

    assert refreshed.status_code == 200
    assert "Refreshed from IOL" in refreshed.text
    assert "Ana Perez" in refreshed.text
    assert "123456" in refreshed.text
    assert "Moderado" in refreshed.text
    # The page states where the data came from and when it was read.
    assert "source:" in refreshed.text
    assert "fetched" in refreshed.text


async def test_the_profile_page_masks_the_document_number(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)
    page = await client.get("/profile")
    await client.post(
        "/profile/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )

    response = await client.get("/profile")

    assert "20315460" not in response.text
    assert "•••••460" in response.text


async def test_the_raw_broker_payload_is_not_dumped_on_the_page(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)
    page = await client.get("/profile")
    await client.post(
        "/profile/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )

    response = await client.get("/profile")

    # Field names from the raw response must not leak into the HTML.
    assert "perfilInversor" not in response.text
    assert "numeroCuenta" not in response.text


async def test_a_failed_refresh_keeps_the_last_snapshot_and_marks_it_stale(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)
    page = await client.get("/profile")
    await client.post(
        "/profile/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )

    fake_iol.profile_error_status = 503
    page = await client.get("/profile")
    stale = await client.post(
        "/profile/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )

    assert stale.status_code == 200
    assert "stale" in stale.text
    # The last good data is still on screen.
    assert "Ana Perez" in stale.text


async def test_a_failed_refresh_with_no_snapshot_says_so(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)
    fake_iol.profile_error_status = 500

    page = await client.get("/profile")
    response = await client.post(
        "/profile/refresh",
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
        "/profile/refresh", data={"csrf_token": "forged"}, follow_redirects=False
    )

    assert response.status_code == 400
    assert fake_iol.token_grants == []


async def test_switching_connections_changes_the_snapshot(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client, label="First account")
    second = await create_connection(client, label="Second account")
    second_id = second.headers["location"].rsplit("/", 1)[1]

    page = await client.get("/profile")
    await client.post(
        "/profile/refresh",
        data={"csrf_token": extract_csrf(page.text), "connection_id": second_id},
        follow_redirects=True,
    )

    # The first connection still has nothing saved.
    first_view = await client.get("/profile")
    assert "No snapshot saved yet" in first_view.text

    second_view = await client.get(f"/profile?connection_id={second_id}")
    assert "Ana Perez" in second_view.text


# --- Account status page --------------------------------------------------


async def test_account_status_page_without_a_connection(
    client, make_user, password: str
) -> None:
    await logged_in_user(client, make_user, password)

    response = await client.get("/account-status")

    assert response.status_code == 200
    assert "No broker connection yet" in response.text


async def test_refreshing_the_account_status_shows_the_balances(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    page = await client.get("/account-status")
    refreshed = await client.post(
        "/account-status/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )

    assert refreshed.status_code == 200
    assert "Refreshed from IOL" in refreshed.text
    assert "200.00" in refreshed.text
    assert "Peso_Argentino" in refreshed.text


async def test_a_failed_account_status_refresh_is_marked_stale(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)
    page = await client.get("/account-status")
    await client.post(
        "/account-status/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )

    fake_iol.account_status_error_status = 500
    page = await client.get("/account-status")
    stale = await client.post(
        "/account-status/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )

    assert "stale" in stale.text
    assert "200.00" in stale.text


# --- Log hygiene ----------------------------------------------------------


async def test_no_password_or_token_reaches_the_logs(
    client, make_user, password: str, fake_iol, caplog
) -> None:
    # Scope the level to the application logger: raising the root level would
    # also switch on the httpx and aiosqlite debug firehose.
    caplog.set_level(logging.DEBUG, logger="ia_trading_broker")
    await logged_in_user(client, make_user, password)
    # These are the credentials actually sent to the broker, so this is the
    # value that a careless log statement would leak.
    await create_connection(
        client, username=fake_iol.username, password=fake_iol.password
    )

    page = await client.get("/profile")
    await client.post(
        "/profile/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )
    status_page = await client.get("/account-status")
    await client.post(
        "/account-status/refresh",
        data={"csrf_token": extract_csrf(status_page.text)},
        follow_redirects=True,
    )

    logs = caplog.text
    # The reads really happened and really were logged, so the assertions
    # below are not passing merely because nothing was recorded.
    assert "snapshot saved for connection id=" in logs
    assert fake_iol.issued_tokens, "the fake broker was expected to issue a token"

    assert fake_iol.password not in logs
    for token in fake_iol.issued_tokens:
        assert token not in logs
