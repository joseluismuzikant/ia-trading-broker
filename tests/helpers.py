"""Shared helpers for the browser-flow tests."""

from __future__ import annotations

import re

from httpx import AsyncClient

CSRF_FIELD_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def extract_csrf(html: str) -> str:
    """Pull the CSRF token out of a rendered form."""
    match = CSRF_FIELD_RE.search(html)
    assert match is not None, "csrf_token field not found in the page"
    return match.group(1)


async def login(client: AsyncClient, username: str, password: str):
    """Log in through the real form, including the double-submit cookie."""
    page = await client.get("/login")
    assert page.status_code == 200
    response = await client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": extract_csrf(page.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, "login did not succeed"
    return response


async def csrf_from_dashboard(client: AsyncClient) -> str:
    """Return the CSRF token of the currently logged-in session."""
    page = await client.get("/dashboard")
    assert page.status_code == 200
    return extract_csrf(page.text)


async def create_connection(
    client: AsyncClient,
    *,
    label: str = "My IOL account",
    username: str = "iol-test-user",
    password: str = "iol-test-password",
    country: str = "argentina",
    follow_redirects: bool = False,
):
    """Submit the new-connection form."""
    page = await client.get("/connections/new")
    assert page.status_code == 200
    return await client.post(
        "/connections",
        data={
            "label": label,
            "username": username,
            "password": password,
            "country": country,
            "csrf_token": extract_csrf(page.text),
        },
        follow_redirects=follow_redirects,
    )
