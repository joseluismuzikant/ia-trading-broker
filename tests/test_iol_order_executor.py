"""Tests for the IOL trading writes: the exact POST, and nothing ambiguous.

The Postman collection documents ``POST /api/v2/operar/Comprar`` and
``POST /api/v2/operar/Vender`` as form-urlencoded with ``mercado``, ``simbolo``,
``cantidad``, ``precio``, ``plazo``, ``validez`` and ``tipoOrden``. A limit
order must never carry ``monto``. HTTP 201 with ``numeroOperacion`` creates the
operation; 202 with ``ok: false`` and 4xx are refusals; a timeout or a 5xx is
unknown and must never be resent.

Every test here runs against an ``httpx.MockTransport``. No real order is ever
possible.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from app.domain.money import money
from app.domain.trading import Order
from app.infrastructure.iol.client import IOLClient
from app.infrastructure.iol.order_executor import (
    IOLExecutionConfigError,
    IOLOrderExecutor,
    broker_status_of,
    live_validity_date,
)


class ScriptedBroker:
    """A tiny transport that answers trading POSTs from a script."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, dict, str]] = []

    def transport(self, request: httpx.Request) -> httpx.Response:
        body = (
            {key: values[0] for key, values in parse_qs(request.content.decode("utf-8")).items()}
            if request.content
            else {}
        )
        self.calls.append((request.url.path, body, request.headers.get("content-type", "")))
        if not self.replies:
            raise AssertionError("the broker received an unexpected extra POST")
        reply = self.replies.pop(0)
        if reply.get("raise") == "timeout":
            raise httpx.ReadTimeout("simulated timeout")
        return httpx.Response(reply.get("status", 200), json=reply.get("json"))


def buy_order(**overrides) -> Order:
    fields = {
        "symbol": "GGAL",
        "side": "BUY",
        "quantity": Decimal("10"),
        "limit_price": money("60.00"),
        "notional": money("600.00"),
        "market": "BCBA",
    }
    fields.update(overrides)
    return Order(**fields)


def executor_for(replies: list[dict], **overrides) -> tuple[IOLOrderExecutor, ScriptedBroker]:
    broker = ScriptedBroker(replies)
    client = IOLClient(
        base_url="http://iol.test",
        timeout=5.0,
        transport=httpx.MockTransport(broker.transport),
    )
    settings = {
        "client": client,
        "access_token": "test-token",
        "plazo": "t2",
        "validity": "2026-12-05T00:00:00",
    }
    settings.update(overrides)
    return IOLOrderExecutor(**settings), broker


# --- The request itself ---------------------------------------------------


async def test_a_buy_posts_to_comprar_as_a_limit_order() -> None:
    executor, broker = executor_for([{"status": 201, "json": {"numeroOperacion": 42}}])

    await executor.submit(buy_order())

    path, body, content_type = broker.calls[0]
    assert path == "/api/v2/operar/Comprar"
    assert content_type.startswith("application/x-www-form-urlencoded")
    assert body["mercado"] == "BCBA"
    assert body["simbolo"] == "GGAL"
    assert body["cantidad"] == "10"
    assert body["precio"] == "60.00"
    assert body["plazo"] == "t2"
    assert body["validez"] == "2026-12-05T00:00:00"
    assert body["tipoOrden"] == "precioLimite"
    # A limit order must never carry an amount.
    assert "monto" not in body


async def test_a_sell_posts_to_vender() -> None:
    executor, broker = executor_for([{"status": 201, "json": {"numeroOperacion": 43}}])

    await executor.submit(buy_order(side="SELL"))

    path, _body, _content_type = broker.calls[0]
    assert path == "/api/v2/operar/Vender"


# --- Outcome classification ----------------------------------------------


async def test_a_created_operation_is_accepted_but_never_filled() -> None:
    executor, _broker = executor_for([{"status": 201, "json": {"numeroOperacion": 2248604}}])

    result = await executor.submit(buy_order())

    assert result.status == "accepted"
    assert result.accepted is True
    assert result.broker_order_id == "2248604"
    assert result.is_fill is False
    assert "not yet reported as filled" in result.message


async def test_a_202_refusal_is_rejected() -> None:
    executor, _broker = executor_for(
        [
            {
                "status": 202,
                "json": {
                    "ok": False,
                    "messages": [{"description": "El papel no opera en el mercado."}],
                },
            }
        ]
    )

    result = await executor.submit(buy_order())

    assert result.status == "rejected"
    assert result.accepted is False
    assert "El papel no opera" in result.message


async def test_a_400_is_rejected_safely() -> None:
    executor, _broker = executor_for(
        [{"status": 400, "json": "Los datos ingresados son incorrectos"}]
    )

    result = await executor.submit(buy_order())

    assert result.status == "rejected"
    assert result.accepted is False


async def test_a_5xx_is_unknown_because_the_outcome_cannot_be_proven() -> None:
    executor, _broker = executor_for([{"status": 500, "json": {"ok": False, "messages": []}}])

    result = await executor.submit(buy_order())

    assert result.status == "unknown"
    assert result.accepted is False


async def test_a_timeout_is_unknown_and_is_never_retried() -> None:
    executor, broker = executor_for([{"raise": "timeout"}])

    result = await executor.submit(buy_order())

    assert result.status == "unknown"
    assert "reconciled" in result.message
    assert len(broker.calls) == 1  # exactly one POST, no automatic retry


# --- Authentication -------------------------------------------------------


async def test_a_token_refusal_may_be_retried_exactly_once() -> None:
    renewed: list[str] = []

    async def renew() -> str:
        renewed.append("yes")
        return "fresh-token"

    executor, broker = executor_for(
        [{"status": 401, "json": {"ok": False}}, {"status": 201, "json": {"numeroOperacion": 7}}],
        renew_token=renew,
    )

    result = await executor.submit(buy_order())

    assert result.status == "accepted"
    assert renewed == ["yes"]
    assert len(broker.calls) == 2


async def test_a_token_refusal_without_a_renewal_is_refused_and_sends_nothing_else() -> None:
    executor, broker = executor_for([{"status": 401, "json": {"ok": False}}])

    result = await executor.submit(buy_order())

    assert result.status == "rejected"
    assert "token" in result.message
    assert len(broker.calls) == 1


# --- Fail-closed configuration -------------------------------------------


async def test_a_missing_market_refuses_to_send_anything() -> None:
    executor, broker = executor_for([{"status": 201, "json": {"numeroOperacion": 1}}])

    with pytest.raises(IOLExecutionConfigError, match="market"):
        await executor.submit(buy_order(market=None))

    assert broker.calls == []


async def test_a_missing_settlement_refuses_to_build() -> None:
    with pytest.raises(IOLExecutionConfigError, match="IOL_LIVE_PLAZO"):
        IOLOrderExecutor(client=None, access_token="t", plazo="", validity="2026-12-05T00:00:00")

    with pytest.raises(IOLExecutionConfigError, match="IOL_LIVE_VALIDITY_DAYS"):
        IOLOrderExecutor(client=None, access_token="t", plazo="t2", validity="")


async def test_a_fractional_quantity_is_refused() -> None:
    executor, broker = executor_for([{"status": 201, "json": {"numeroOperacion": 1}}])

    with pytest.raises(IOLExecutionConfigError, match="whole-unit"):
        await executor.submit(buy_order(quantity=Decimal("1.5")))

    assert broker.calls == []


def test_the_validity_date_comes_from_configuration_not_from_a_guess() -> None:
    from datetime import date

    assert live_validity_date(1, today=date(2026, 10, 4)) == "2026-10-05T00:00:00"
    with pytest.raises(IOLExecutionConfigError, match="IOL_LIVE_VALIDITY_DAYS"):
        live_validity_date(0)


# --- Reconciliation -------------------------------------------------------


def test_a_broker_operation_document_is_mapped_conservatively() -> None:
    assert (
        broker_status_of(
            {
                "estadoActual": "Operada",
                "fechaOperado": "2026-10-04T00:00:00",
                "cantidadOperada": 10,
            },
            quantity=Decimal("10"),
        ).status
        == "filled"
    )
    assert broker_status_of({"estadoActual": "cancelada_Por_Vencimiento"}).status == "cancelled"
    state = broker_status_of({"estadoActual": "en_Preparacion"})
    assert state.status == "submitted"
    state = broker_status_of({"estadoActual": "en_Modificacion"})
    assert state.status is None
    assert "en_Modificacion" in state.message


async def test_a_bond_trade_sends_the_bond_symbol_and_market() -> None:
    executor, broker = executor_for([{"status": 201, "json": {"numeroOperacion": 55}}])

    await executor.submit(buy_order(symbol="AL30", quantity=Decimal("3")))

    path, body, _content_type = broker.calls[0]
    assert path == "/api/v2/operar/Comprar"
    assert body["simbolo"] == "AL30"
    assert body["mercado"] == "BCBA"
    assert body["cantidad"] == "3"
    assert body["tipoOrden"] == "precioLimite"


def test_a_partial_execution_is_never_reported_as_a_complete_fill() -> None:
    state = broker_status_of(
        {
            "estadoActual": "Parcialmente_Operada",
            "fechaOperado": "2026-10-07T15:00:00",
            "cantidadOperada": 40,
            "precioOperado": 61.25,
        },
        quantity=Decimal("100"),
    )

    assert state.status == "partially_filled"
    assert state.filled_quantity == Decimal("40")
    assert state.filled_price == money("61.25")


def test_an_execution_price_is_never_invented_from_the_limit() -> None:
    state = broker_status_of(
        {"estadoActual": "Operada", "fechaOperado": "2026-10-07T15:00:00"},
        quantity=Decimal("10"),
    )

    assert state.status == "filled"
    assert state.filled_price is None
    assert "No execution price" in state.message
