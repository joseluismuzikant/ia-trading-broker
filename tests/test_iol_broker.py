"""Tests for IOL as a BrokerReader: what it returns, and how it authenticates.

Everything runs against the fake IOL transport, so no real call is possible.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.portfolio import Portfolio
from app.infrastructure.iol import IOLBroker, IOLClient
from app.infrastructure.iol.broker import balances_from_status
from app.infrastructure.iol.schemas import AccountStatus
from app.ports.broker_reader import BrokerReader, CashBalance
from tests.fakes import FakeIOL


def broker_for(fake: FakeIOL, *, password: str | None = None) -> tuple[IOLBroker, list[int]]:
    """An IOLBroker on the fake transport, and a counter of password decryptions."""
    decryptions: list[int] = []

    def provide() -> str:
        decryptions.append(1)
        return password if password is not None else fake.password

    client = IOLClient(base_url="http://iol.test", timeout=5.0, transport=fake.transport)
    return (
        IOLBroker(
            connection_id=1, username=fake.username, password_provider=provide, client=client
        ),
        decryptions,
    )


def test_iol_broker_satisfies_the_port() -> None:
    broker, _ = broker_for(FakeIOL())
    assert isinstance(broker, BrokerReader)


async def test_authenticate_logs_in_and_reads_nothing(fake_iol) -> None:
    broker, decryptions = broker_for(fake_iol)

    await broker.authenticate()
    await broker.aclose()

    assert fake_iol.token_grants == ["password"]
    assert fake_iol.authenticated_paths == []
    assert decryptions == [1]


async def test_a_wrong_password_is_a_broker_error_without_the_password(fake_iol) -> None:
    from app.ports.broker_reader import BrokerError

    broker, _ = broker_for(fake_iol, password="not-the-right-password")

    with pytest.raises(BrokerError) as caught:
        await broker.authenticate()

    assert "not-the-right-password" not in str(caught.value)
    await broker.aclose()


async def test_the_password_is_not_decrypted_while_a_token_is_cached(fake_iol) -> None:
    broker, decryptions = broker_for(fake_iol)

    await broker.authenticate()
    await broker.get_balances()
    await broker.get_balances()
    await broker.aclose()

    assert decryptions == [1]
    assert fake_iol.token_grants == ["password"]


async def test_the_portfolio_comes_back_in_the_shared_format_without_cash(fake_iol) -> None:
    broker, _ = broker_for(fake_iol)

    portfolio = await broker.get_portfolio("argentina")
    await broker.aclose()

    assert isinstance(portfolio, Portfolio)
    assert portfolio.country == "argentina"
    assert portfolio.cash is None  # IOL's holdings endpoint does not report it
    assert portfolio.positions[0].instrument.symbol == "GGAL"
    assert portfolio.positions[0].total_quantity == 100


async def test_balances_are_free_cash_per_currency(fake_iol) -> None:
    broker, _ = broker_for(fake_iol)

    balances = await broker.get_balances()
    await broker.aclose()

    assert balances == [CashBalance(currency="ARS", available=Decimal("100.0"))]


def test_two_accounts_in_one_currency_are_added_and_unreported_cash_is_skipped() -> None:
    status = AccountStatus.model_validate(
        {
            "cuentas": [
                {"numero": "1", "moneda": "peso_Argentino", "disponible": 100.5},
                {"numero": "2", "moneda": "Peso_Argentino", "disponible": 50},
                {"numero": "3", "moneda": "dolar_Estadounidense", "disponible": 12.25},
                {"numero": "4", "moneda": "peso_Argentino"},
            ]
        }
    )

    assert balances_from_status(status) == [
        CashBalance("ARS", Decimal("150.5")),
        CashBalance("USD", Decimal("12.25")),
    ]


async def test_history_is_neutral_bars_in_the_requested_window(fake_iol) -> None:
    broker, _ = broker_for(fake_iol)

    bars = await broker.get_history(symbol="ggal", market="bcba", days=30)
    await broker.aclose()

    assert len(bars) == 45
    assert bars[0].close is not None and bars[0].at
    history_path = [p for p in fake_iol.authenticated_paths if "seriehistorica" in p][0]
    assert "/BCBA/" in history_path and "/GGAL/" in history_path


async def test_an_operation_is_read_by_its_number(fake_iol) -> None:
    fake_iol.operations["123"] = {"numero": 123, "estadoActual": "terminada"}
    broker, _ = broker_for(fake_iol)

    operation = await broker.get_operation("123")
    await broker.aclose()

    assert operation["numero"] == 123


async def test_a_rejected_token_is_renewed_once_and_the_read_is_retried(fake_iol) -> None:
    fake_iol.reject_authenticated_requests = 1
    broker, _ = broker_for(fake_iol)

    balances = await broker.get_balances()
    await broker.aclose()

    assert balances
    assert fake_iol.token_grants == ["password", "refresh_token"]


async def test_a_second_rejection_is_not_retried_again(fake_iol) -> None:
    from app.infrastructure.iol import IOLAuthError

    fake_iol.reject_authenticated_requests = 2
    broker, _ = broker_for(fake_iol)

    with pytest.raises(IOLAuthError):
        await broker.get_balances()
    await broker.aclose()


async def test_a_failed_refresh_falls_back_to_a_login(fake_iol) -> None:
    fake_iol.reject_authenticated_requests = 1
    fake_iol.accept_refresh_tokens = False
    broker, _ = broker_for(fake_iol)

    assert await broker.get_balances()
    await broker.aclose()

    assert fake_iol.token_grants == ["password", "refresh_token", "password"]


async def test_the_profile_and_account_pages_keep_iols_own_payload(fake_iol) -> None:
    broker, _ = broker_for(fake_iol)

    profile = await broker.get_profile_payload()
    account = await broker.get_account_status_payload()
    await broker.aclose()

    assert profile["nombre"] == "Ana"
    assert "cuentas" in account
