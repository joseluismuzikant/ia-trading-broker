"""Tests for the country portfolio: client calls, mapping, and the read path."""

from __future__ import annotations

import httpx
import pytest

from app.domain.portfolio import Portfolio
from app.infrastructure.iol.client import IOLClient
from app.infrastructure.iol.errors import IOLNotFoundError, IOLResponseError
from app.infrastructure.iol.mapping import to_portfolio
from app.infrastructure.iol.schemas import IOLPortfolio
from app.models import portfolio_snapshot_kind
from app.services import broker as broker_service
from app.services import connections as connections_service
from app.services import snapshots as snapshots_service
from tests.fakes import FakeIOL, load_fixture

USERNAME = "iol-test-user"
PASSWORD = "iol-test-password"


def build_client(fake: FakeIOL) -> IOLClient:
    """An IOL client wired to the fake transport."""
    return IOLClient(base_url="http://iol.test", timeout=5.0, transport=fake.transport)


async def make_connection(db_session, user, fake: FakeIOL):
    """Save a connection whose credentials match the fake broker."""
    return await connections_service.create_connection(
        db_session,
        user_id=user.id,
        label="My IOL account",
        username=fake.username,
        password=fake.password,
    )


def raw_portfolio() -> IOLPortfolio:
    """The fixture parsed into the IOL response model."""
    return IOLPortfolio.model_validate(load_fixture("portfolio_ok.json"))


# --- Client ---------------------------------------------------------------


async def test_get_portfolio_parses_the_assets() -> None:
    fake = FakeIOL()

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        portfolio = await client.get_portfolio(token.access_token, "argentina")

    assert portfolio.pais == "Argentina"
    assert len(portfolio.activos) == 2
    assert portfolio.activos[0].titulo.simbolo == "GGAL"
    assert portfolio.activos[0].ultimo_precio == 60.555
    assert fake.authenticated_paths == ["/api/v2/portafolio/argentina"]


async def test_get_quote_parses_the_last_price() -> None:
    fake = FakeIOL()

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        quote = await client.get_quote(
            token.access_token, market="BCBA", symbol="GGAL"
        )

    assert quote.ultimo_precio == 60.555
    assert quote.moneda == "Peso_Argentino"
    assert fake.authenticated_paths == ["/api/v2/BCBA/Titulos/GGAL/Cotizacion"]


async def test_get_price_history_returns_a_list() -> None:
    fake = FakeIOL()

    async with build_client(fake) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        history = await client.get_price_history(
            token.access_token,
            market="BCBA",
            symbol="GGAL",
            date_from="2024-01-01",
            date_to="2024-01-31",
        )

    assert len(history) == 2
    assert history[0].ultimo_precio == 60.555
    assert "seriehistorica" in fake.authenticated_paths[0]


async def test_an_unknown_instrument_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={"access_token": "t", "token_type": "bearer", "expires_in": 900},
            )
        return httpx.Response(404, json={"error": "not found"})

    async with IOLClient(
        base_url="http://iol.test", transport=httpx.MockTransport(handler)
    ) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        with pytest.raises(IOLNotFoundError):
            await client.get_quote(token.access_token, market="BCBA", symbol="NOPE")


async def test_a_transient_server_error_is_retried_once_then_succeeds() -> None:
    state = {"attempts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={"access_token": "t", "token_type": "bearer", "expires_in": 900},
            )
        state["attempts"] += 1
        if state["attempts"] == 1:
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return httpx.Response(200, json=load_fixture("portfolio_ok.json"))

    async with IOLClient(
        base_url="http://iol.test", transport=httpx.MockTransport(handler)
    ) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        portfolio = await client.get_portfolio(token.access_token, "argentina")

    assert state["attempts"] == 2
    assert len(portfolio.activos) == 2


async def test_a_server_error_that_never_clears_gives_up() -> None:
    state = {"attempts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={"access_token": "t", "token_type": "bearer", "expires_in": 900},
            )
        state["attempts"] += 1
        return httpx.Response(500, json={"error": "boom"})

    async with IOLClient(
        base_url="http://iol.test", transport=httpx.MockTransport(handler)
    ) as client:
        token = await client.fetch_token(username=USERNAME, password=PASSWORD)
        with pytest.raises(IOLResponseError):
            await client.get_portfolio(token.access_token, "argentina")

    assert state["attempts"] == 3


# --- Mapping to the shared format ----------------------------------------


def test_positions_carry_total_and_available_quantities() -> None:
    portfolio = to_portfolio("argentina", raw_portfolio(), cash=100.0)

    first = portfolio.positions[0]
    assert first.instrument.symbol == "GGAL"
    assert first.instrument.market == "BCBA"
    assert first.instrument.instrument_type == "ACCIONES"
    assert first.instrument.currency == "Peso_Argentino"
    assert first.total_quantity == 100.0
    assert first.committed_quantity == 25.0
    # 100 held, 25 committed, so 75 are free to trade.
    assert first.available_quantity == 75.0
    assert first.last_price == 60.555
    assert first.average_price == 50.0
    assert first.market_value == 6055.5


def test_a_missing_quantity_stays_unknown() -> None:
    raw = IOLPortfolio.model_validate(
        {"pais": "Argentina", "activos": [{"titulo": {"simbolo": "GGAL"}}]}
    )

    position = to_portfolio("argentina", raw).positions[0]

    assert position.total_quantity is None
    assert position.available_quantity is None
    assert position.instrument.symbol == "GGAL"


def test_the_portfolio_totals_cash_and_positions() -> None:
    portfolio = to_portfolio("argentina", raw_portfolio(), cash=100.0)

    assert portfolio.positions_value == 18055.5
    assert portfolio.total_value == 18155.5
    assert portfolio.currency == "Peso_Argentino"


def test_without_cash_the_total_is_unknown() -> None:
    portfolio = to_portfolio("argentina", raw_portfolio())

    assert portfolio.cash is None
    assert portfolio.total_value is None


def test_the_shared_format_survives_a_json_round_trip() -> None:
    # Snapshots are stored as JSON, so the format must survive a round trip.
    portfolio = to_portfolio("argentina", raw_portfolio(), cash=100.0)

    restored = Portfolio.model_validate(portfolio.model_dump(mode="json"))

    assert restored == portfolio
    assert restored.positions[0].available_quantity == 75.0


# --- Read path ------------------------------------------------------------


async def test_a_portfolio_read_saves_the_shared_format(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    result = await broker_service.read_portfolio(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    assert result.ok is True
    assert result.snapshot.kind == portfolio_snapshot_kind("argentina")
    payload = result.snapshot.payload
    assert payload["country"] == "argentina"
    assert payload["currency"] == "Peso_Argentino"
    assert payload["positions"][0]["instrument"]["symbol"] == "GGAL"


async def test_a_single_refresh_costs_one_broker_call(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    await broker_service.read_portfolio(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    # A login plus exactly one read: pages never poll, so refreshing is cheap.
    assert fake_iol.token_grants == ["password"]
    assert fake_iol.authenticated_paths == ["/api/v2/portafolio/argentina"]


async def test_cash_comes_from_the_saved_account_status(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    await broker_service.read_account_status(
        db_session, user_id=user.id, connection=connection
    )

    result = await broker_service.read_portfolio(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    # The fixture account holds 100.0 available in Peso_Argentino.
    assert result.snapshot.payload["cash"] == 100.0


async def test_cash_is_unknown_without_an_account_status(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    result = await broker_service.read_portfolio(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    assert result.snapshot.payload["cash"] is None


async def test_each_country_has_its_own_snapshot(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    await broker_service.read_portfolio(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    await broker_service.read_portfolio(
        db_session, user_id=user.id, connection=connection, country="estados_unidos"
    )

    stored = await snapshots_service.count_snapshots(
        db_session, user_id=user.id, connection_id=connection.id
    )
    assert stored == 2
    assert fake_iol.authenticated_paths == [
        "/api/v2/portafolio/argentina",
        "/api/v2/portafolio/estados_unidos",
    ]


async def test_a_failed_refresh_keeps_the_last_snapshot_stale(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    good = await broker_service.read_portfolio(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    fake_iol.portfolio_error_status = 503

    failed = await broker_service.read_portfolio(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    assert failed.ok is False
    assert failed.is_stale is True
    assert failed.snapshot.id == good.snapshot.id
    assert failed.snapshot.is_stale is True
    assert failed.snapshot.payload["positions"][0]["instrument"]["symbol"] == "GGAL"


async def test_an_unsupported_country_is_rejected_before_any_call(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    with pytest.raises(connections_service.ConnectionValidationError):
        await broker_service.read_portfolio(
            db_session, user_id=user.id, connection=connection, country="atlantis"
        )

    assert fake_iol.token_grants == []
