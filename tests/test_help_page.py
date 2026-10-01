"""Tests for the public help and glossary page."""

from __future__ import annotations

from tests.helpers import login

PASSWORD = "correct-horse-battery-staple"


async def test_the_help_page_is_public(client) -> None:
    # No login, and no redirect to the login page: the page is open.
    response = await client.get("/help", follow_redirects=False)

    assert response.status_code == 200
    assert "Help and glossary" in response.text
    assert 'href="/login"' in response.text


async def test_the_help_page_explains_the_key_concepts(client) -> None:
    page = await client.get("/help")

    for term in [
        "Paper ledger",
        "EMA",
        "RSI",
        "MACD",
        "ATR",
        "Snapshot",
        "Proposal",
        "Glossary",
        "call budget",
    ]:
        assert term in page.text, term


async def test_the_help_page_covers_how_the_project_works(client) -> None:
    page = await client.get("/help")

    assert "How a run works" in page.text
    assert "How the code is organised" in page.text
    assert "Sizing and risk checks" in page.text


async def test_the_nav_links_to_help_before_signing_in(client) -> None:
    page = await client.get("/login")

    assert 'href="/help"' in page.text


async def test_the_nav_links_to_help_once_signed_in(client, make_user) -> None:
    await make_user("alice")
    await login(client, "alice", PASSWORD)

    page = await client.get("/dashboard")

    assert page.status_code == 200
    assert 'href="/help"' in page.text
    assert "Log out" in page.text


async def test_the_help_link_is_active_on_its_own_page(client, make_user) -> None:
    await make_user("alice")
    await login(client, "alice", PASSWORD)

    page = await client.get("/help")

    assert page.status_code == 200
    assert "nav__link--active" in page.text
    assert 'aria-current="page"' in page.text


async def test_the_current_page_is_marked_in_the_nav(client, make_user) -> None:
    await make_user("alice")
    await login(client, "alice", PASSWORD)

    page = await client.get("/analysis")

    assert page.status_code == 200
    assert "nav__link--active" in page.text


async def test_the_help_page_never_shows_a_password(client, make_user) -> None:
    await make_user("alice")
    await login(client, "alice", PASSWORD)

    page = await client.get("/help")

    assert PASSWORD not in page.text
