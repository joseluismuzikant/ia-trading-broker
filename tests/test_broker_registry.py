"""Tests for choosing a broker adapter and for the saved price-history format."""

from __future__ import annotations

import pytest

from app.infrastructure.iol import IOLBroker
from app.ports.broker_reader import BrokerUnavailableError, PriceBar
from app.services import broker as broker_service
from app.services import snapshots as snapshots_service
from tests.day5_helpers import make_connection, uptrend_quotes


async def _connection_of(db, user, fake_iol, broker: str):
    connection = await make_connection(db, user, fake_iol)
    connection.broker = broker
    await db.commit()
    return connection


async def test_an_iol_connection_gets_the_iol_adapter(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    reader = await broker_service.open_reader(connection)

    assert isinstance(reader, IOLBroker)
    await reader.aclose()


async def test_a_broker_without_an_adapter_is_refused(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await _connection_of(db_session, user, fake_iol, "unknown")

    with pytest.raises(BrokerUnavailableError, match="not supported yet"):
        await broker_service.open_reader(connection)


async def test_a_read_of_an_unsupported_broker_is_a_clear_result_not_a_crash(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await _connection_of(db_session, user, fake_iol, "unknown")

    result = await broker_service.read_portfolio(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    assert result.ok is False
    assert "not supported yet" in (result.error or "")
    assert fake_iol.token_grants == []


async def test_live_trading_is_refused_for_a_broker_that_is_not_wired(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await _connection_of(db_session, user, fake_iol, "unknown")

    with pytest.raises(BrokerUnavailableError):
        await broker_service.trading_session(connection)


async def test_a_connection_test_goes_through_the_adapter(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    result = await broker_service.test_connection(db_session, connection=connection)

    assert result.ok is True
    assert fake_iol.token_grants == ["password"]


async def test_a_new_history_snapshot_stores_neutral_bars(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    result = await broker_service.read_price_history(
        db_session, user_id=user.id, connection=connection, market="BCBA", symbol="GGAL"
    )

    assert result.ok
    first = result.snapshot.payload["quotes"][0]
    assert set(first) == {"at", "close", "high", "low", "volume"}
    bars = broker_service.bars_from_snapshot(result.snapshot)
    assert all(isinstance(bar, PriceBar) for bar in bars) and len(bars) == 45


async def test_a_snapshot_saved_in_iols_old_format_still_loads(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    legacy = await snapshots_service.save_snapshot(
        db_session,
        user_id=user.id,
        connection_id=connection.id,
        kind="price_history:GGAL",
        payload={
            "symbol": "GGAL",
            "market": "BCBA",
            "quotes": [quote.model_dump(mode="json", by_alias=True) for quote in uptrend_quotes()],
        },
    )

    bars = broker_service.bars_from_snapshot(legacy)

    assert len(bars) == 45
    assert bars[0].close is not None and bars[0].at
