"""The order-execution port.

An :class:`OrderExecutor` accepts one prepared :class:`~app.domain.trading.Order`
and answers with an :class:`~app.domain.trading.OrderResult`. It is the only
seam through which the workflow reaches an execution venue, so the paper
simulator used on Day 5 and a real broker adapter on a later day share it.

The port says nothing about the broker, the account, or the network. A paper
executor needs no credentials; a live executor would.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain.trading import Order, OrderResult


@runtime_checkable
class OrderExecutor(Protocol):
    """Submit one order and report what the venue did with it.

    Implementations must be safe to call for an order that was already
    submitted: the caller persists the order before calling, and uses the saved
    row, not this method, to decide whether a resubmit is allowed.
    """

    async def submit(self, order: Order) -> OrderResult:
        """Send one order and return the venue's answer."""
        ...
