"""Live order submission against the PPI trading API.

:class:`PPIOrderExecutor` implements the same
:class:`~app.ports.execution.OrderExecutor` contract as the IOL and paper
executors, so the execution service cannot tell them apart.

What it guarantees:

* limit orders only (``operationType = PRECIO-LIMITE``), whole units, a
  positive price;
* the instrument type PPI needs comes from an explicit map. A symbol with no
  confirmed type is refused instead of guessed, which is how bonds and CEDEARs
  stay blocked until their quantity and price conventions are verified;
* the settlement and the order term come from deployment configuration; when
  they are missing the executor refuses to build, fail-closed;
* ``Order/Budget`` runs first and places nothing. An order PPI describes
  differently from what was requested, or one that carries a mandatory
  disclaimer, is refused: a person must read a disclaimer, an adapter must not
  click through it;
* ``Order/Confirm`` is sent once. The application's idempotency key travels as
  ``externalID`` so a lost answer can be found again at PPI. A token refusal
  proves nothing was placed, so it alone is retried, exactly once.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from app.domain.trading import Order, OrderResult
from app.infrastructure.ppi.client import (
    TRADE_ACCEPTED,
    TRADE_AUTH,
    TRADE_REJECTED,
    PPIClient,
    TradeAck,
)
from app.infrastructure.ppi.errors import PPIAPIError, PPIAuthError
from app.infrastructure.ppi.schemas import PPIOrderDocument

logger = logging.getLogger("ia_trading_broker.ppi.orders")

#: Values for a limit order on a share. The quantity type, the order term and
#: the operation names are members of PPI's ``Configuration/QuantityTypes``,
#: ``OperationTerms`` and ``Operations`` lists; the order type comes from the
#: Postman collection.
QUANTITY_TYPE_UNITS = "PAPELES"
ORDER_TYPE_LIMIT = "PRECIO-LIMITE"
OPERATION_BUY = "COMPRA"
OPERATION_SELL = "VENTA"


class PPIExecutionConfigError(ValueError):
    """Live PPI execution cannot send this order: a parameter is missing."""


class PPIOrderExecutor:
    """Submit prepared orders to PPI exactly once and classify the answer."""

    def __init__(
        self,
        *,
        client: PPIClient,
        access_token: str,
        account_number: str,
        settlement: str,
        operation_term: str,
        instrument_type_for: Callable[[str], str | None],
        renew_token: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        for value, name in (
            (account_number, "the PPI account number"),
            (settlement, "PPI_LIVE_SETTLEMENT"),
            (operation_term, "PPI_LIVE_OPERATION_TERM"),
        ):
            if not value.strip():
                raise PPIExecutionConfigError(
                    f"{name} is not configured; live orders cannot be sent."
                )
        self._client = client
        self._access_token = access_token
        self._account = account_number.strip()
        self._settlement = settlement.strip()
        self._term = operation_term.strip()
        self._instrument_type_for = instrument_type_for
        self._renew_token = renew_token

    async def submit(self, order: Order) -> OrderResult:
        """Budget, then confirm one order, and return the classified outcome."""
        request = self._request_for(order)

        try:
            budget = await self._budget(request)
        except PPIAPIError as exc:
            # The budget places nothing, so a failure here is a clean refusal.
            return OrderResult(
                status="rejected",
                accepted=False,
                message=f"The order was not sent: {exc.message}",
            )
        problem = _budget_problem(order, budget)
        if problem:
            return OrderResult(
                status="rejected", accepted=False, message=f"The order was not sent: {problem}"
            )
        request["disclaimers"] = [item.model_dump(mode="json") for item in budget.disclaimers]

        ack = await self._client.confirm(self._access_token, request)
        if ack.status == TRADE_AUTH and self._renew_token is not None:
            logger.info("token refused for %s %s; renewing once", order.side, order.symbol)
            self._access_token = await self._renew_token()
            ack = await self._client.confirm(self._access_token, request)
        return self._to_result(order, ack)

    async def _budget(self, request: dict[str, Any]) -> PPIOrderDocument:
        try:
            return await self._client.budget(self._access_token, request)
        except PPIAuthError:
            if self._renew_token is None:
                raise
            self._access_token = await self._renew_token()
            return await self._client.budget(self._access_token, request)

    def _request_for(self, order: Order) -> dict[str, Any]:
        instrument_type = self._instrument_type_for(order.symbol)
        if not instrument_type:
            raise PPIExecutionConfigError(
                f"{order.symbol} has no confirmed PPI instrument type; the order was not sent."
            )
        if order.quantity <= 0 or order.quantity != order.quantity.to_integral_value():
            raise PPIExecutionConfigError(
                f"Live orders need a whole-unit quantity, got {order.quantity}."
            )
        if not order.limit_price.is_positive:
            raise PPIExecutionConfigError("Live orders need a positive limit price.")
        if not (order.external_id or "").strip():
            raise PPIExecutionConfigError(
                "The order has no idempotency key to send as externalID; it was not sent."
            )
        return {
            "accountNumber": self._account,
            "quantity": int(order.quantity),
            # A float only at the wire: PPI's JSON takes a number.
            "price": float(order.limit_price.amount),
            "ticker": order.symbol,
            "instrumentType": instrument_type,
            "quantityType": QUANTITY_TYPE_UNITS,
            "operationType": ORDER_TYPE_LIMIT,
            "operationTerm": self._term,
            "operationMaxDate": None,
            "operation": OPERATION_BUY if order.side == "BUY" else OPERATION_SELL,
            "Settlement": self._settlement,
            "externalID": order.external_id,
            "disclaimers": [],
        }

    def _to_result(self, order: Order, ack: TradeAck) -> OrderResult:
        if ack.status == TRADE_ACCEPTED:
            return OrderResult(
                status="accepted",
                accepted=True,
                broker_order_id=ack.order_id,
                message=(
                    f"The broker accepted {order.side} {order.quantity} {order.symbol} "
                    f"(order {ack.order_id}). It is created, not yet reported as filled."
                ),
            )
        if ack.status == TRADE_REJECTED:
            return OrderResult(
                status="rejected",
                accepted=False,
                message=ack.message or "The broker refused the order.",
            )
        if ack.status == TRADE_AUTH:
            return OrderResult(
                status="rejected",
                accepted=False,
                message="The broker refused the order because the token was not usable; nothing was sent.",
            )
        return OrderResult(
            status="unknown",
            accepted=False,
            message=ack.message or "The outcome of the order is unknown.",
        )


def _budget_problem(order: Order, budget: PPIOrderDocument) -> str:
    """Why the budget must not be confirmed, or an empty string."""
    mandatory = [item for item in budget.disclaimers if item.mandatory]
    if mandatory:
        codes = ", ".join(item.code or "?" for item in mandatory)
        return f"PPI requires a disclaimer to be read and accepted by a person ({codes})."
    if budget.quantity is not None and budget.quantity != order.quantity:
        return f"PPI priced {budget.quantity} units, not the {order.quantity} requested."
    if budget.price is not None and budget.price != Decimal(order.limit_price.amount):
        return f"PPI priced the order at {budget.price}, not the requested {order.limit_price}."
    if budget.ticker is not None and budget.ticker.upper() != order.symbol.upper():
        return f"PPI priced {budget.ticker}, not {order.symbol}."
    return ""
