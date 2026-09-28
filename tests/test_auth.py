"""End-to-end tests for login, logout, sessions, and CSRF protection."""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

CSRF_FIELD_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def extract_csrf(html: str) -> str:
    """Pull the CSRF token out of a rendered form."""
    match = CSRF_FIELD_RE.search(html)
    assert match is not None, "csrf_token field not found in the page"
    return match.group(1)


async def load_login_form(client: AsyncClient) -> str:
    """Open the login page and return its CSRF token."""
    response = await client.get("/login")
    assert response.status_code == 200
    return extract_csrf(response.text)


async def login(
    client: AsyncClient,
    username: str,
    password: str,
) -> object:
    """Submit the login form with a valid CSRF token."""
    csrf_token = await load_login_form(client)
    return await client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": csrf_token},
        follow_redirects=False,
    )


# --- Anonymous access -----------------------------------------------------


async def test_root_redirects_anonymous_user_to_login(client: AsyncClient) -> None:
    response = await client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_dashboard_requires_login(client: AsyncClient) -> None:
    response = await client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_login_page_sets_csrf_cookie(client: AsyncClient) -> None:
    response = await client.get("/login")
    assert response.status_code == 200
    assert "iatb_csrf" in client.cookies
    assert 'name="csrf_token"' in response.text


# --- Login ----------------------------------------------------------------


async def test_login_with_valid_credentials_redirects_to_dashboard(
    client: AsyncClient, make_user, password: str
) -> None:
    await make_user("alice", password)

    response = await login(client, "alice", password)

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    assert "iatb_session" in client.cookies


async def test_session_cookie_is_httponly_and_samesite(
    client: AsyncClient, make_user, password: str
) -> None:
    await make_user("alice", password)

    response = await login(client, "alice", password)
    set_cookie = response.headers["set-cookie"]

    assert "iatb_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie


async def test_dashboard_shows_the_logged_in_user(
    client: AsyncClient, make_user, password: str
) -> None:
    await make_user("alice", password)
    await login(client, "alice", password)

    response = await client.get("/dashboard")

    assert response.status_code == 200
    assert "alice" in response.text


async def test_wrong_password_returns_generic_error(
    client: AsyncClient, make_user, password: str
) -> None:
    await make_user("alice", password)

    response = await login(client, "alice", "not-the-password")

    assert response.status_code == 401
    assert "Invalid username or password" in response.text
    assert "iatb_session" not in client.cookies


async def test_unknown_user_returns_the_same_generic_error(client: AsyncClient) -> None:
    response = await login(client, "nobody", "whatever-password")

    assert response.status_code == 401
    assert "Invalid username or password" in response.text


async def test_inactive_user_cannot_log_in(
    client: AsyncClient, make_user, password: str
) -> None:
    await make_user("alice", password, is_active=False)

    response = await login(client, "alice", password)

    assert response.status_code == 401
    assert "iatb_session" not in client.cookies


async def test_username_is_case_insensitive(
    client: AsyncClient, make_user, password: str
) -> None:
    await make_user("Alice", password)

    response = await login(client, "ALICE", password)

    assert response.status_code == 303


# --- CSRF -----------------------------------------------------------------


async def test_login_without_csrf_token_is_rejected(client: AsyncClient, make_user, password: str) -> None:
    await make_user("alice", password)
    await client.get("/login")

    response = await client.post(
        "/login",
        data={"username": "alice", "password": password, "csrf_token": ""},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "iatb_session" not in client.cookies


async def test_login_with_wrong_csrf_token_is_rejected(
    client: AsyncClient, make_user, password: str
) -> None:
    await make_user("alice", password)
    await client.get("/login")

    response = await client.post(
        "/login",
        data={"username": "alice", "password": password, "csrf_token": "forged-token"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "iatb_session" not in client.cookies


async def test_logout_without_csrf_token_does_not_revoke_the_session(
    client: AsyncClient, make_user, password: str
) -> None:
    await make_user("alice", password)
    await login(client, "alice", password)

    await client.post("/logout", data={"csrf_token": "forged-token"}, follow_redirects=False)

    # The session survives, so the dashboard is still reachable.
    assert (await client.get("/dashboard", follow_redirects=False)).status_code == 200


# --- Logout ---------------------------------------------------------------


async def test_logout_revokes_the_session(
    client: AsyncClient, make_user, password: str
) -> None:
    await make_user("alice", password)
    await login(client, "alice", password)

    dashboard = await client.get("/dashboard")
    csrf_token = extract_csrf(dashboard.text)

    response = await client.post(
        "/logout", data={"csrf_token": csrf_token}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert (await client.get("/dashboard", follow_redirects=False)).status_code == 303


async def test_logged_in_user_visiting_login_is_sent_to_dashboard(
    client: AsyncClient, make_user, password: str
) -> None:
    await make_user("alice", password)
    await login(client, "alice", password)

    response = await client.get("/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


# --- Session isolation ----------------------------------------------------


async def test_second_user_gets_a_separate_session(
    client: AsyncClient, make_user, password: str
) -> None:
    await make_user("alice", password)
    await make_user("bob", password)

    await login(client, "alice", password)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as second:
        await login(second, "bob", password)

        alice_page = await client.get("/dashboard")
        bob_page = await second.get("/dashboard")

        assert "alice" in alice_page.text
        assert "bob" not in alice_page.text
        assert "bob" in bob_page.text
        assert "alice" not in bob_page.text
        assert client.cookies["iatb_session"] != second.cookies["iatb_session"]


async def test_unknown_session_cookie_is_rejected(client: AsyncClient) -> None:
    client.cookies.set("iatb_session", "not-a-real-session-token")

    response = await client.get("/dashboard", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_root_redirects_logged_in_user_to_dashboard(
    client: AsyncClient, make_user, password: str
) -> None:
    await make_user("alice", password)
    await login(client, "alice", password)

    response = await client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


# --- Error page -----------------------------------------------------------


async def test_unknown_route_renders_the_error_page(client: AsyncClient) -> None:
    response = await client.get("/definitely-not-a-page")

    assert response.status_code == 404
    assert "Error 404" in response.text


@pytest.mark.parametrize("path", ["/static/style.css"])
async def test_static_files_are_served(client: AsyncClient, path: str) -> None:
    response = await client.get(path)

    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
