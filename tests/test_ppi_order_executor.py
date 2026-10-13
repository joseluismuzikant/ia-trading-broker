"""Tests for PPI order submission: the exact requests, and nothing ambiguous.

Budget runs first and places nothing; Confirm is sent once. Confirm is
classified strictly: an order id is accepted, a 4xx is rejected, a refused
token proves nothing was placed, and a timeout, a 5xx or a 200 without an id is
unknown and must never be resent. Every test uses ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.domain.money import money
from app.domain.trading import Order
from app.infrastructure.ppi.client import PPIClient
from app.infrastructure.ppi.order_executor import PPIExecutionConfigError, PPIOrderExecutor


class Broker:
    """Answers Budget and Confirm from scripts and records every call."""

    def __init__(self, *, budget=None, confirm=None) -> None:
        self.budget = budget or [lambda request: self.echo_budget(request)]
        self.confirm = confirm or [httpx.Response(200, json={"id": 4321, "status": "x"})]
        self.calls: list[tuple[str, dict, str]] = []

    @staticmethod
    def echo_budget(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "ticker": body["ticker"],
                "quantity": body["quantity"],
                "price": body["price"],
                "disclaimers": [],
            },
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.url.path, body, request.headers.get("Authorization", "")))
        if request.url.path.endswith("/Order/Budget"):
            step = self.budget.pop(0) if len(self.budget) > 1 else self.budget[0]
            return step(request) if callable(step) else step
        assert request.url.path.endswith("/Order/Confirm"), request.url.path
        step = self.confirm.pop(0) if len(self.confirm) > 1 else self.confirm[0]
        if isinstance(step, Exception):
            raise step
        return step

    @property
    def confirms(self) -> list[tuple[str, dict, str]]:
        return [call for call in self.calls if call[0].endswith("/Order/Confirm")]


def executor_for(broker: Broker, **overrides) -> PPIOrderExecutor:
    client = PPIClient(
        base_url="https://ppi.test",
        authorized_client="a",
        client_key="k",
        transport=httpx.MockTransport(broker),
    )
    options = {
        "client": client,
        "access_token": "tok",
        "account_number": "ACC1",
        "settlement": "INMEDIATA",
        "operation_term": "HASTA-SU-EJECUCIÓN",
        "instrument_type_for": lambda symbol: "ACCIONES" if symbol == "GGAL" else None,
    }
    options.update(overrides)
    return PPIOrderExecutor(**options)


def order(side="BUY", symbol="GGAL", quantity="10", price="240.50", external="proposal:1:BUY:GGAL"):
    return Order(
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        limit_price=money(price),
        notional=money(Decimal(quantity) * Decimal(price)),
        execution_mode="LIVE",
        external_id=external,
    )


async def test_a_buy_is_budgeted_then_confirmed_once() -> None:
    broker = Broker()
    result = await executor_for(broker).submit(order())

    assert [path.rsplit("/", 1)[1] for path, _, _ in broker.calls] == ["Budget", "Confirm"]
    _, body, auth = broker.confirms[0]
    assert auth == "Bearer tok"
    assert body["accountNumber"] == "ACC1"
    assert body["quantity"] == 10
    assert body["price"] == 240.5
    assert body["ticker"] == "GGAL"
    assert body["instrumentType"] == "ACCIONES"
    assert body["quantityType"] == "PAPELES"
    assert body["operationType"] == "PRECIO-LIMITE"
    assert body["operation"] == "COMPRA"
    assert body["Settlement"] == "INMEDIATA"
    assert body["operationTerm"] == "HASTA-SU-EJECUCIÓN"
    assert body["externalID"] == "proposal:1:BUY:GGAL"
    assert result.status == "accepted"
    assert result.accepted is True
    assert result.broker_order_id == "4321"
    assert result.filled_quantity == 0


async def test_a_sell_uses_the_sell_operation() -> None:
    broker = Broker()
    await executor_for(broker).submit(order(side="SELL", external="proposal:1:SELL:GGAL"))
    assert broker.confirms[0][1]["operation"] == "VENTA"


async def test_a_mandatory_disclaimer_stops_before_confirm() -> None:
    disclaimer = {"code": "RISK1", "description": "Read me", "mandatory": True}
    broker = Broker(budget=[httpx.Response(200, json={"disclaimers": [disclaimer]})])
    result = await executor_for(broker).submit(order())

    assert result.status == "rejected"
    assert "RISK1" in result.message
    assert broker.confirms == []


async def test_a_budget_that_changes_the_order_stops_before_confirm() -> None:
    broker = Broker(budget=[httpx.Response(200, json={"ticker": "GGAL", "quantity": 99})])
    result = await executor_for(broker).submit(order())

    assert result.status == "rejected"
    assert broker.confirms == []


async def test_a_budget_failure_sends_nothing() -> None:
    broker = Broker(budget=[httpx.Response(500, json={})])
    result = await executor_for(broker).submit(order())

    assert result.status == "rejected"
    assert "not sent" in result.message
    assert broker.confirms == []


async def test_a_refusal_is_rejected() -> None:
    broker = Broker(confirm=[httpx.Response(400, json={"message": "Insufficient funds"})])
    result = await executor_for(broker).submit(order())

    assert result.status == "rejected"
    assert result.accepted is False
    assert "Insufficient funds" in result.message


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("down"),
        httpx.Response(502, json={}),
        httpx.Response(200, json={"status": "ok"}),
        httpx.Response(200, content=b"not json"),
    ],
)
async def test_an_ambiguous_answer_is_unknown_and_sent_once(answer) -> None:
    broker = Broker(confirm=[answer])
    result = await executor_for(broker).submit(order())

    assert result.status == "unknown"
    assert result.accepted is False
    assert len(broker.confirms) == 1


async def test_a_refused_token_is_renewed_and_the_order_resent_once() -> None:
    broker = Broker(confirm=[httpx.Response(401, json={}), httpx.Response(200, json={"id": 7})])
    renewals: list[str] = []

    async def renew() -> str:
        renewals.append("renewed")
        return "fresh"

    result = await executor_for(broker, renew_token=renew).submit(order())

    assert renewals == ["renewed"]
    assert [auth for _, _, auth in broker.confirms] == ["Bearer tok", "Bearer fresh"]
    assert result.status == "accepted"


async def test_a_refused_token_without_a_renewer_places_nothing() -> None:
    broker = Broker(confirm=[httpx.Response(403, json={})])
    result = await executor_for(broker).submit(order())

    assert result.status == "rejected"
    assert "nothing was sent" in result.message
    assert len(broker.confirms) == 1


async def test_a_second_refusal_is_not_retried_again() -> None:
    broker = Broker(confirm=[httpx.Response(401, json={})])

    async def renew() -> str:
        return "fresh"

    result = await executor_for(broker, renew_token=renew).submit(order())

    assert result.status == "rejected"
    assert len(broker.confirms) == 2


@pytest.mark.parametrize(
    "bad",
    [
        order(symbol="AL30"),  # no confirmed PPI instrument type
        order(quantity="1.5"),
        order(quantity="0"),
        order(price="0"),
        order(external=""),
    ],
)
async def test_an_order_that_cannot_be_sent_safely_is_refused_before_any_call(bad) -> None:
    broker = Broker()
    with pytest.raises(PPIExecutionConfigError):
        await executor_for(broker).submit(bad)
    assert broker.calls == []


@pytest.mark.parametrize("field", ["settlement", "operation_term", "account_number"])
def test_missing_terms_make_the_executor_refuse_to_build(field) -> None:
    with pytest.raises(PPIExecutionConfigError):
        executor_for(Broker(), **{field: " "})
