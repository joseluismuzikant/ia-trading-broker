"""Live order submission against the IOL trading API.

:class:`IOLOrderExecutor` implements the same :class:`~app.ports.execution.OrderExecutor`
contract the paper simulator does, so the execution service cannot tell the
difference between the venue types except by which implementation it was given.
The LangGraph workflow never sees IOL HTTP details: it calls the execution
service, which calls this executor.

What it guarantees:

* limit orders only (``tipoOrden = precioLimite`` with ``cantidad`` and
  ``precio``, never ``monto``), exactly as the IOL documentation describes;
* the market is taken from the order's own instrument metadata, never guessed
  from the ticker;
* the settlement (``plazo``) and the order validity (``validez``) are supplied
  explicitly at construction from deployment configuration. When they are not
  configured the executor refuses to build, fail-closed;
* one POST per order, never retried blindly. The result is classified strictly:
  HTTP 201 with ``numeroOperacion`` is ``accepted`` (created at the broker, not
  necessarily filled); an explicit refusal is ``rejected``; a timeout, a
  transport failure, or a 5xx is ``unknown`` and must be reconciled;
* a token refusal (401/403) proves the broker placed nothing, so it is safe to
  renew the token once and retry exactly once — and nothing else ever retries.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.domain.money import Money, money
from app.domain.trading import Order, OrderResult
from app.infrastructure.iol.client import (
    ORDER_LIMIT,
    TRADE_ACCEPTED,
    TRADE_AUTH,
    TRADE_REJECTED,
    IOLClient,
    TradeAck,
)

logger = logging.getLogger("ia_trading_broker.iol.orders")


class IOLExecutionConfigError(ValueError):
    """Live execution is enabled but a required IOL parameter is not set."""


def live_validity_date(validity_days: int | None, *, today: date | None = None) -> str:
    """The ``validez`` timestamp IOL expects: good until that date."""
    if not validity_days:
        raise IOLExecutionConfigError(
            "IOL_LIVE_VALIDITY_DAYS is not configured; live orders cannot be sent."
        )
    day = (today or date.today()) + timedelta(days=validity_days)
    return f"{day.isoformat()}T00:00:00"


class IOLOrderExecutor:
    """Submit prepared orders to IOL exactly once and classify the answer."""

    def __init__(
        self,
        *,
        client: IOLClient,
        access_token: str,
        plazo: str,
        validity: str,
        renew_token: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        if not plazo.strip():
            raise IOLExecutionConfigError(
                "IOL_LIVE_PLAZO is not configured; live orders cannot be sent."
            )
        if not validity.strip():
            raise IOLExecutionConfigError(
                "IOL_LIVE_VALIDITY_DAYS is not configured; live orders cannot be sent."
            )
        self._client = client
        self._access_token = access_token
        self._plazo = plazo.strip()
        self._validity = validity.strip()
        self._renew_token = renew_token

    async def submit(self, order: Order) -> OrderResult:
        """Send one order to IOL and return the strictly classified outcome."""
        market = (order.market or "").strip()
        if not market:
            raise IOLExecutionConfigError(
                f"No market is known for {order.symbol}; the order was not sent."
            )
        if order.quantity <= 0 or order.quantity != order.quantity.to_integral_value():
            raise IOLExecutionConfigError(
                f"Live orders need a whole-unit quantity, got {order.quantity}."
            )
        if not order.limit_price.is_positive:
            raise IOLExecutionConfigError("Live orders need a positive limit price.")

        ack = await self._send(order, market=market)
        if ack.status == TRADE_AUTH and self._renew_token is not None:
            # 401/403 proves the broker placed nothing, so exactly one renewal
            # and one resend cannot duplicate an order. Nothing else retries.
            logger.info("token refused for %s %s; renewing once", order.side, order.symbol)
            self._access_token = await self._renew_token()
            ack = await self._send(order, market=market)
        return self._to_result(order, ack)

    async def _send(self, order: Order, *, market: str) -> TradeAck:
        call = self._client.place_buy if order.side == "BUY" else self._client.place_sell
        return await call(
            self._access_token,
            market=market,
            symbol=order.symbol,
            quantity=str(int(order.quantity)),
            price=str(order.limit_price.amount),
            plazo=self._plazo,
            validity=self._validity,
            order_type=ORDER_LIMIT,
        )

    def _to_result(self, order: Order, ack: TradeAck) -> OrderResult:
        if ack.status == TRADE_ACCEPTED:
            return OrderResult(
                status="accepted",
                accepted=True,
                broker_order_id=ack.operation_id,
                message=(
                    f"The broker accepted {order.side} {order.quantity} "
                    f"{order.symbol} (operation {ack.operation_id}). It is "
                    "created, not yet reported as filled."
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
                message=(
                    "The broker refused the order because the token was not "
                    "usable; nothing was sent."
                ),
            )
        return OrderResult(
            status="unknown",
            accepted=False,
            message=ack.message or "The outcome of the order is unknown.",
        )


@dataclass(frozen=True)
class OperationState:
    """What one IOL operation document proves about an order.

    ``status`` is ``None`` when the document changes nothing. ``filled_price``
    is only ever a price the broker reported — never the requested limit.
    """

    status: str | None
    filled_quantity: Decimal | None
    filled_price: Money | None
    message: str


def broker_status_of(operation: dict, *, quantity: Decimal | None = None) -> OperationState:
    """Map one IOL operation document to what it actually proves.

    Only evidence in the document is used: the operated quantity and price
    (``cantidadOperada`` / ``precioOperado``) decide fills, and the state text
    only decides refusals and cancellations. A partial fill is never reported
    as a complete fill, and an execution price is never invented from the
    limit price.
    """
    state = str(operation.get("estadoActual") or "").strip()
    lowered = state.lower()
    operated_at = operation.get("fechaOperado")

    def _number(field: str) -> Decimal | None:
        value = operation.get(field)
        if value is None or isinstance(value, bool):
            return None
        try:
            return Decimal(str(value))
        except Exception:  # noqa: BLE001 - unparseable broker numbers are absent
            return None

    operated_quantity = _number("cantidadOperada")
    operated_price_raw = _number("precioOperado")
    operated_price = money(operated_price_raw) if operated_price_raw else None

    if "rechazad" in lowered:
        return OperationState(
            status="rejected",
            filled_quantity=operated_quantity,
            filled_price=operated_price,
            message=f"The broker reports the operation as rejected ({state}).",
        )
    if "cancelad" in lowered:
        return OperationState(
            status="cancelled",
            filled_quantity=operated_quantity,
            filled_price=operated_price,
            message=f"The broker reports the operation as cancelled ({state}).",
        )

    if operated_quantity is not None and operated_quantity > 0:
        if quantity is not None and operated_quantity < quantity:
            return OperationState(
                status="partially_filled",
                filled_quantity=operated_quantity,
                filled_price=operated_price,
                message=(
                    f"The broker reports {operated_quantity} of {quantity} "
                    f"units executed ({state or 'partial'}); the rest is still "
                    "working."
                ),
            )
        return OperationState(
            status="filled",
            filled_quantity=operated_quantity,
            filled_price=operated_price,
            message=(
                f"The broker reports {operated_quantity} unit(s) executed"
                + (f" at {operated_price}." if operated_price else "; no execution price reported.")
            ),
        )

    if "parcial" in lowered:
        return OperationState(
            status="partially_filled",
            filled_quantity=operated_quantity,
            filled_price=operated_price,
            message=f"The broker reports a partial execution ({state}).",
        )
    if "operad" in lowered and operated_at:
        return OperationState(
            status="filled",
            filled_quantity=operated_quantity,
            filled_price=operated_price,
            message=(
                f"The broker reports the operation as executed ({state})."
                + ("" if operated_price else " No execution price was reported.")
            ),
        )
    if any(
        word in lowered for word in ("pendiente", "ingresada", "preparacion", "abierta", "vigente")
    ):
        return OperationState(
            status="submitted",
            filled_quantity=None,
            filled_price=None,
            message=f"The broker reports the operation as pending ({state}).",
        )
    return OperationState(
        status=None,
        filled_quantity=operated_quantity,
        filled_price=operated_price,
        message=f"The broker reports the state {state or 'unknown'}; the order is unchanged.",
    )
