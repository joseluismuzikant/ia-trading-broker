"""Tests for PPI as a BrokerReader: what it reads, and what it refuses to guess.

Everything runs against an httpx mock transport shaped like PPI's documented API,
whose examples are illustrative, so these pin this adapter's behaviour and not
PPI's. No PPI endpoint is reachable.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.infrastructure.ppi.broker import PPIBroker
from app.infrastructure.ppi.client import PPIClient
from app.infrastructure.ppi.errors import PPIAuthError
from app.ports.broker_reader import (
    BrokerReader,
    BrokerUnavailableError,
    CashBalance,
)
from app.services import broker as broker_service
from app.services import connections as connections_service
from tests.test_connection_brokers import PPI_FIELDS, make_ppi

TOKEN = {"accessToken": "access-1", "refreshToken": "refresh-1", "tokenType": "bearer"}
BALANCES = [
    {"name": "Pesos", "simbol": "ARS", "amount": 1500.5, "settlement": "INMEDIATA"},
    {"name": "Pesos", "simbol": "ARS", "amount": 999.0, "settlement": "A-48HS"},
    {"name": "Dolares", "simbol": "USD", "amount": 20, "settlement": "INMEDIATA"},
]
HISTORY = [
    {"date": "2026-10-01T00:00:00Z", "price": 100.5, "max": 101, "min": 99, "volume": 1000},
    {"date": "2026-10-02T00:00:00Z", "price": 102.0, "max": 103, "min": 100, "volume": 900},
]


class FakePPI:
    """A scriptable PPI: records calls, can reject tokens."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.reject_tokens = 0
        self.refresh_ok = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/1.0")
        self.calls.append((request.method, path))
        if path == "/Account/LoginApi":
            return httpx.Response(200, json=TOKEN)
        if path == "/Account/RefreshToken":
            if not self.refresh_ok:
                return httpx.Response(401, json={})
            return httpx.Response(200, json={**TOKEN, "accessToken": "access-2"})
        if self.reject_tokens > 0:
            self.reject_tokens -= 1
            return httpx.Response(401, json={})
        if path == "/Account/AvailableBalance":
            return httpx.Response(200, json=BALANCES)
        if path == "/MarketData/Search":
            return httpx.Response(200, json=HISTORY)
        if path == "/Order/Detail":
            return httpx.Response(200, json={"id": 77, "status": "x", "ticker": "GGAL"})
        return httpx.Response(404, json={})


def broker_for(fake: FakePPI) -> tuple[PPIBroker, list[int]]:
    keys: list[int] = []

    def provide() -> tuple[str, str]:
        keys.append(1)
        return "public", "private"

    client = PPIClient(
        base_url="https://ppi.test",
        authorized_client="a",
        client_key="k",
        transport=httpx.MockTransport(fake),
    )
    return (
        PPIBroker(
            connection_id=900,
            account_number="ACC1",
            credentials_provider=provide,
            client=client,
            instrument_type_for=lambda symbol: "ACCIONES" if symbol == "GGAL" else None,
        ),
        keys,
    )


def test_the_ppi_broker_satisfies_the_port() -> None:
    broker, _ = broker_for(FakePPI())
    assert isinstance(broker, BrokerReader)


async def test_authenticating_logs_in_once_and_reuses_the_token() -> None:
    fake = FakePPI()
    broker, keys = broker_for(fake)

    await broker.authenticate()
    await broker.get_balances()
    await broker.get_balances()
    await broker.aclose()

    assert [p for _, p in fake.calls].count("/Account/LoginApi") == 1
    assert keys == [1]


async def test_only_cash_that_settles_at_once_is_available() -> None:
    broker, _ = broker_for(FakePPI())

    balances = await broker.get_balances()
    await broker.aclose()

    assert balances == [
        CashBalance("ARS", Decimal("1500.5")),
        CashBalance("USD", Decimal("20")),
    ]


async def test_history_is_neutral_bars_for_a_confirmed_instrument_type() -> None:
    fake = FakePPI()
    broker, _ = broker_for(fake)

    bars = await broker.get_history(symbol="ggal", market="BCBA", days=30)
    await broker.aclose()

    assert [bar.close for bar in bars] == [100.5, 102.0]
    assert bars[0].high == 101 and bars[0].low == 99 and bars[0].volume == 1000
    assert ("GET", "/MarketData/Search") in fake.calls


async def test_an_instrument_type_that_is_not_confirmed_is_refused_before_any_call() -> None:
    fake = FakePPI()
    broker, _ = broker_for(fake)

    with pytest.raises(BrokerUnavailableError, match="not confirmed"):
        await broker.get_history(symbol="AL30", market="BCBA", days=30)
    await broker.aclose()

    assert fake.calls == []


async def test_positions_are_refused_because_ppi_does_not_document_them() -> None:
    broker, _ = broker_for(FakePPI())

    with pytest.raises(BrokerUnavailableError, match="holdings"):
        await broker.get_portfolio("argentina")
    await broker.aclose()


async def test_an_order_is_read_by_its_id() -> None:
    broker, _ = broker_for(FakePPI())

    operation = await broker.get_operation("77")
    await broker.aclose()

    assert operation["id"] == 77


async def test_a_rejected_token_is_renewed_once_and_the_read_is_retried() -> None:
    fake = FakePPI()
    fake.reject_tokens = 1
    broker, _ = broker_for(fake)

    assert await broker.get_balances()
    await broker.aclose()

    paths = [p for _, p in fake.calls]
    assert "/Account/RefreshToken" in paths
    assert paths.count("/Account/AvailableBalance") == 2


async def test_a_failed_refresh_falls_back_to_a_login() -> None:
    fake = FakePPI()
    fake.reject_tokens = 1
    fake.refresh_ok = False
    broker, keys = broker_for(fake)

    assert await broker.get_balances()
    await broker.aclose()

    assert [p for _, p in fake.calls].count("/Account/LoginApi") == 2
    assert keys == [1, 1]


async def test_a_second_rejection_is_not_retried_again() -> None:
    fake = FakePPI()
    fake.reject_tokens = 2
    broker, _ = broker_for(fake)

    with pytest.raises(PPIAuthError):
        await broker.get_balances()
    await broker.aclose()


# --- Through the registry ------------------------------------------------------------


@pytest.fixture
def ppi_service(monkeypatch):
    fake = FakePPI()
    monkeypatch.setattr(
        broker_service,
        "build_ppi_client",
        lambda: PPIClient(
            base_url="https://ppi.test",
            authorized_client="a",
            client_key="k",
            transport=httpx.MockTransport(fake),
        ),
    )
    return fake


async def test_a_ppi_connection_gets_the_ppi_adapter(db_session, make_user, ppi_service) -> None:
    user = await make_user("alice")
    connection = await make_ppi(db_session, user)

    reader = await broker_service.open_reader(connection, db=db_session)
    try:
        assert isinstance(reader, PPIBroker)
        await reader.authenticate()
    finally:
        await reader.aclose()

    assert connections_service.load_credentials(connection) == PPI_FIELDS


async def test_testing_a_ppi_connection_logs_in_with_the_saved_keys(
    db_session, make_user, ppi_service
) -> None:
    user = await make_user("alice")
    connection = await make_ppi(db_session, user)

    result = await broker_service.test_connection(db_session, connection=connection)

    assert result.ok is True
    assert ("POST", "/Account/LoginApi") in ppi_service.calls


async def test_a_ppi_portfolio_read_fails_closed_with_a_clear_reason(
    db_session, make_user, ppi_service
) -> None:
    user = await make_user("alice")
    connection = await make_ppi(db_session, user)

    result = await broker_service.read_portfolio(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    assert result.ok is False
    assert "holdings" in (result.error or "")


async def test_ppi_without_its_integration_settings_says_so(db_session, make_user) -> None:
    user = await make_user("alice")
    connection = await make_ppi(db_session, user)

    result = await broker_service.test_connection(db_session, connection=connection)

    assert result.ok is False
    assert "PPI_AUTHORIZED_CLIENT" in (result.error or "")


async def test_live_trading_is_not_available_for_ppi(db_session, make_user, ppi_service) -> None:
    user = await make_user("alice")
    connection = await make_ppi(db_session, user)

    with pytest.raises(BrokerUnavailableError, match="not available"):
        await broker_service.trading_session(connection)
