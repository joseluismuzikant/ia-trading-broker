"""The paper execution adapter.

It fills an order locally at its limit price and returns a settled
:class:`~app.domain.trading.OrderResult`. It never contacts a broker and never
reads or writes the database: the caller saves the order before calling this, and
applies the returned fill to the paper ledger afterwards. That keeps this adapter
tiny and makes it the one place that would be swapped for a real broker.
"""

from __future__ import annotations

import logging

from app.domain.money import Money, quantity
from app.domain.trading import Order, OrderResult
from app.ports.execution import OrderExecutor

logger = logging.getLogger("ia_trading_broker.paper")


class PaperOrderExecutor(OrderExecutor):
    """Fill paper orders immediately and completely at the limit price.

    A paper fill is intentionally simple: the order is accepted and filled in
    full at the price the policy already decided. Slippage and partial fills are
    a later task, and are why the executor returns a structured result instead of
    a boolean.
    """

    async def submit(self, order: Order) -> OrderResult:
        """Accept and fill one paper order at its limit price."""
        if order.quantity <= 0:
            return OrderResult(
                status="rejected",
                accepted=False,
                message="A paper order needs a positive quantity.",
            )
        if not order.limit_price.is_positive:
            return OrderResult(
                status="rejected",
                accepted=False,
                message="A paper order needs a positive limit price.",
            )

        filled = quantity(order.quantity)
        notional = Money(order.limit_price.amount * filled)
        logger.info("paper fill symbol=%s side=%s qty=%s", order.symbol, order.side, filled)
        return OrderResult(
            status="filled",
            accepted=True,
            filled_quantity=filled,
            filled_price=order.limit_price,
            filled_notional=notional,
            broker_order_id=None,
            message="Paper order filled in full at the limit price.",
        )
