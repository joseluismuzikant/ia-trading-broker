"""Browser-flow tests for the analysis page."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.helpers import create_connection, extract_csrf, login


async def logged_in_user(client, make_user, password: str, username: str = "alice"):
    """Create a user, log in, and return the user row."""
    user = await make_user(username, password)
    await login(client, username, password)
    return user


async def save_a_portfolio(client) -> None:
    """Refresh the portfolio so the ledger has something to copy."""
    page = await client.get("/portfolio")
    await client.post(
        "/portfolio/refresh",
        data={"csrf_token": extract_csrf(page.text)},
        follow_redirects=True,
    )


async def create_the_ledger(client) -> None:
    """Create the paper ledger through the page."""
    page = await client.get("/analysis")
    await client.post(
        "/analysis/ledger",
        data={"csrf_token": extract_csrf(page.text), "country": "argentina"},
        follow_redirects=True,
    )


# --- Access control -------------------------------------------------------


async def test_analysis_page_requires_login(client) -> None:
    response = await client.get("/analysis", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_create_ledger_requires_login(client) -> None:
    response = await client.post("/analysis/ledger", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_run_analysis_requires_login(client) -> None:
    response = await client.post("/analysis/run", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- Empty states ---------------------------------------------------------


async def test_analysis_page_without_a_connection(client, make_user, password: str) -> None:
    await logged_in_user(client, make_user, password)

    response = await client.get("/analysis")

    assert response.status_code == 200
    assert "No broker connection yet" in response.text


async def test_analysis_page_before_a_ledger(client, make_user, password: str, fake_iol) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    response = await client.get("/analysis")

    assert response.status_code == 200
    assert "No portfolio data yet" in response.text
    assert "No proposal saved yet" in response.text


async def test_an_unsupported_country_is_rejected(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    response = await client.get("/analysis?country=atlantis")

    assert response.status_code == 400


# --- Creating the ledger --------------------------------------------------


async def test_creating_a_ledger_from_the_portfolio(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)
    await save_a_portfolio(client)

    page = await client.get("/analysis")
    response = await client.post(
        "/analysis/ledger",
        data={"csrf_token": extract_csrf(page.text), "country": "argentina"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Simulated portfolio created" in response.text
    assert "GGAL" in response.text
    assert "YPFD" in response.text


async def test_creating_a_ledger_without_a_portfolio_is_reported(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    page = await client.get("/analysis")
    response = await client.post(
        "/analysis/ledger",
        data={"csrf_token": extract_csrf(page.text), "country": "argentina"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Could not create the simulated portfolio" in response.text


# --- Running an analysis --------------------------------------------------


async def test_running_an_analysis_shows_a_proposal(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)
    await save_a_portfolio(client)
    await create_the_ledger(client)

    page = await client.get("/analysis")
    response = await client.post(
        "/analysis/run",
        data={"csrf_token": extract_csrf(page.text), "country": "argentina"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Analysis complete" in response.text
    assert "PENDING" in response.text
    assert "HUMAN_IN_THE_LOOP" in response.text
    assert "LIVE" in response.text
    assert "Turnover" in response.text
    # The page shows the model's target allocation and the trade plan.
    assert "Model target allocation" in response.text
    assert "YPF S.A." in response.text


async def test_a_lowercase_market_still_loads_history(
    client, make_user, password: str, fake_iol
) -> None:
    # IOL reports the portfolio market lowercased. It has to be normalised
    # before the quote path is built, otherwise the history read 404s and the
    # analysis holds with zero observations while claiming "not enough history".
    fake_iol.portfolio["activos"][0]["titulo"]["mercado"] = "bcba"

    await logged_in_user(client, make_user, password)
    await create_connection(client)
    await save_a_portfolio(client)
    await create_the_ledger(client)

    page = await client.get("/analysis")
    response = await client.post(
        "/analysis/run",
        data={"csrf_token": extract_csrf(page.text), "country": "argentina"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Analysis complete" in response.text
    assert "Not enough history" not in response.text
    assert "No price history" not in response.text


async def test_a_failed_history_read_is_reported(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)
    await save_a_portfolio(client)
    await create_the_ledger(client)
    fake_iol.quote_error_status = 500

    page = await client.get("/analysis")
    response = await client.post(
        "/analysis/run",
        data={"csrf_token": extract_csrf(page.text), "country": "argentina"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "No price history for" in response.text


async def test_running_without_a_ledger_creates_one_automatically(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    page = await client.get("/analysis")
    assert "No portfolio data yet" in page.text
    response = await client.post(
        "/analysis/run",
        data={"csrf_token": extract_csrf(page.text), "country": "argentina"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Analysis complete" in response.text
    # The run created the paper ledger from the current portfolio by itself.
    assert "Cash after" in response.text
    assert "GGAL" in response.text


async def test_a_run_without_a_csrf_token_is_rejected(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    response = await client.post(
        "/analysis/run",
        data={"csrf_token": "forged", "country": "argentina"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert fake_iol.token_grants == []


# --- Tenant isolation -----------------------------------------------------


async def test_another_user_cannot_open_the_analysis(client, make_user, password: str) -> None:
    await logged_in_user(client, make_user, password, username="alice")
    created = await create_connection(client, label="Alice account")
    connection_id = created.headers["location"].rsplit("/", 1)[1]

    await make_user("bob", password)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as bob:
        await login(bob, "bob", password)
        response = await bob.get(f"/analysis?connection_id={connection_id}", follow_redirects=False)

    assert response.status_code == 404


# --- Choosing the model for a run -----------------------------------------


async def test_the_run_form_lists_the_configured_models(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)

    page = await client.get("/analysis")

    assert page.status_code == 200
    assert 'name="model"' in page.text
    for label in (
        "GPT-6.1 Sol",
        "Gemini 3.8 Flash",
        "MiMo V2.6 Pro",
        "DeepSeek R1",
        "Grok 4",
        "Fake simulator (smoke test)",
    ):
        assert label in page.text


async def test_an_unknown_model_is_refused_and_no_proposal_is_saved(
    client, make_user, password: str, fake_iol
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)
    await save_a_portfolio(client)
    await create_the_ledger(client)

    page = await client.get("/analysis")
    response = await client.post(
        "/analysis/run",
        data={
            "csrf_token": extract_csrf(page.text),
            "country": "argentina",
            "model": "gpt-9-sol",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "unknown analysis model" in response.text
    assert "No proposal saved yet" in response.text


async def test_the_run_stores_and_shows_the_model_target_allocation(
    client, make_user, password: str, fake_iol, fake_advisor
) -> None:
    await logged_in_user(client, make_user, password)
    await create_connection(client)
    await save_a_portfolio(client)
    await create_the_ledger(client)

    page = await client.get("/analysis")
    response = await client.post(
        "/analysis/run",
        data={
            "csrf_token": extract_csrf(page.text),
            "country": "argentina",
            "model": "deepseek_r1",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Model target allocation" in response.text
    assert "DeepSeek R1" in response.text
    assert "(test advice)" in response.text
    # The fake was asked exactly once, with the whole account.
    assert len(fake_advisor.contexts) == 1


async def test_the_fake_simulator_runs_without_any_provider_key(
    client, make_user, password: str, fake_iol, real_advisors
) -> None:
    """The whole flow, with the catalog simulator and no key configured."""
    await logged_in_user(client, make_user, password)
    await create_connection(client)
    await save_a_portfolio(client)
    await create_the_ledger(client)

    page = await client.get("/analysis")
    response = await client.post(
        "/analysis/run",
        data={
            "csrf_token": extract_csrf(page.text),
            "country": "argentina",
            "model": "fake_simulator",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Model target allocation" in response.text
    assert "Fake simulator (smoke test)" in response.text
    assert "simulator / fake-portfolio-advisor" in response.text
    assert "(test advice)" in response.text


async def test_a_missing_key_stops_the_run_without_a_proposal(
    client, make_user, password: str, fake_iol, monkeypatch, real_advisors
) -> None:
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "openai_api_key", None)

    await logged_in_user(client, make_user, password)
    await create_connection(client)
    await save_a_portfolio(client)
    await create_the_ledger(client)

    page = await client.get("/analysis")
    response = await client.post(
        "/analysis/run",
        data={
            "csrf_token": extract_csrf(page.text),
            "country": "argentina",
            "model": "gpt_6_1_sol",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "The analysis could not be completed" in response.text
    assert "OPENAI_API_KEY" in response.text
    assert "No proposal saved yet" in response.text
