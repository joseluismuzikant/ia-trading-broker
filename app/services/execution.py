"""Paper order submission: the bridge from an approved proposal to a fill.

This module is the only place that calls an :class:`OrderExecutor`. It follows a
fixed, safe order of work for every item in the plan:

1. save the order (``prepared``) before anything else, keyed for idempotency;
2. call the executor once and record its answer;
3. when the fill is confirmed, update the paper ledger with a new snapshot;
4. write one event for each step.

An order that already exists is never sent again, including a ``prepared`` row:
a crash may have happened after submission but before its result was saved. The
existing row is left for reconciliation rather than risking a duplicate. An
``unknown`` result is reported so the caller stops rather than guessing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.trading import ExecutionMode, Order, OrderResult, Proposal, Recommendation
from app.infrastructure.simulation import PaperOrderExecutor
from app.models import (
    EVENT_LEDGER,
    EVENT_ORDER,
    BrokerConnection,
    OrderRecord,
    ProposalRecord,
)
from app.ports import OrderExecutor
from app.services import events as events_service
from app.services import ledger as ledger_service
from app.services import orders as orders_service

logger = logging.getLogger("ia_trading_broker.execution")


class ExecutionError(Exception):
    """A proposal cannot be executed safely."""


@dataclass
class ExecutionReport:
    """What a submission pass did, for the run summary and the tests."""

    submitted: int = 0
    filled: int = 0
    rejected: int = 0
    unknown: int = 0
    skipped: int = 0
    records: list[OrderRecord] = field(default_factory=list)

    @property
    def needs_reconciliation(self) -> bool:
        """True when at least one order ended in an unknown state."""
        return self.unknown > 0


def order_from_recommendation(
    recommendation: Recommendation, execution_mode: ExecutionMode
) -> Order:
    """Build a prepared order from a sized, risk-checked recommendation."""
    assert recommendation.limit_price is not None
    assert recommendation.notional is not None
    return Order(
        symbol=recommendation.symbol,
        side=recommendation.action,  # type: ignore[arg-type]
        quantity=recommendation.quantity,
        limit_price=recommendation.limit_price,
        notional=recommendation.notional,
        status="prepared",
        execution_mode=execution_mode,
        created_at=datetime.now(UTC),
    )


def apply_result(order: Order, result: OrderResult) -> Order:
    """Fold an executor result back into the order model."""
    return order.model_copy(
        update={
            "status": result.status,
            "broker_order_id": result.broker_order_id,
            "filled_quantity": (
                result.filled_quantity if result.is_fill else order.filled_quantity
            ),
            "filled_price": result.filled_price,
            "filled_notional": result.filled_notional,
            "message": result.message or order.message,
            "updated_at": datetime.now(UTC),
        }
    )


async def submit_proposal_orders(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    proposal_record: ProposalRecord,
    executor: OrderExecutor | None = None,
) -> ExecutionReport:
    """Submit every order in a proposal exactly once, in paper mode."""
    proposal = Proposal.model_validate(proposal_record.payload)
    if proposal.execution_mode != "PAPER":
        raise ExecutionError("Live execution is disabled; only PAPER is supported.")
    if (
        proposal_record.user_id != user_id
        or proposal_record.connection_id != connection.id
    ):
        raise ExecutionError("The proposal does not belong to this connection.")

    executor = executor or PaperOrderExecutor()
    report = ExecutionReport()

    ledger = await ledger_service.load_ledger(
        db, user_id=user_id, connection=connection, country=proposal.country
    )
    if ledger is None:
        raise ExecutionError("No paper ledger is available for this proposal.")

    for recommendation in proposal.orders:
        order = order_from_recommendation(recommendation, proposal.execution_mode)
        record, created = await orders_service.prepare_order(
            db,
            user_id=user_id,
            connection_id=connection.id,
            proposal_id=proposal_record.id,
            country=proposal.country,
            order=order,
        )
        report.records.append(record)

        if not created:
            # Never resubmit an existing row. Even ``prepared`` is ambiguous:
            # the process may have stopped after submission but before saving
            # the executor's result.
            report.skipped += 1
            if record.status in {"prepared", "submitted", "accepted", "unknown"}:
                report.unknown += 1
                break
            continue

        await events_service.record_event(
            db,
            user_id=user_id,
            connection_id=connection.id,
            category=EVENT_ORDER,
            event_type="order.prepared",
            message=(
                f"Prepared {order.side} {order.quantity} {order.symbol} "
                f"at {order.limit_price}."
            ),
            proposal_id=proposal_record.id,
            order_id=record.id,
            mode=proposal.execution_mode,
        )

        result = await executor.submit(order)
        report.submitted += 1
        settled = apply_result(order, result)
        await orders_service.update_order(db, record, settled)

        await events_service.record_event(
            db,
            user_id=user_id,
            connection_id=connection.id,
            category=EVENT_ORDER,
            event_type=f"order.{result.status}",
            message=f"{order.side} {order.symbol}: {result.message}",
            proposal_id=proposal_record.id,
            order_id=record.id,
            mode=proposal.execution_mode,
        )

        if result.status == "unknown":
            report.unknown += 1
            logger.warning(
                "order id=%s ended unknown; it will not be resubmitted",
                record.id,
            )
            break

        if result.is_fill:
            ledger = ledger_service.apply_fill(
                ledger,
                order,
                filled_quantity=result.filled_quantity,
                filled_price=result.filled_price or order.limit_price,
            )
            await ledger_service.save_ledger(
                db, user_id=user_id, connection=connection, ledger=ledger
            )
            await events_service.record_event(
                db,
                user_id=user_id,
                connection_id=connection.id,
                category=EVENT_LEDGER,
                event_type="ledger.updated",
                message=(
                    f"Paper ledger updated after the {order.symbol} fill; "
                    f"cash is now {ledger.cash}."
                ),
                proposal_id=proposal_record.id,
                order_id=record.id,
                mode=proposal.execution_mode,
            )
            report.filled += 1
        elif result.status == "rejected":
            report.rejected += 1
            break
        else:
            # A non-terminal or malformed answer is ambiguous. Persist it as
            # unknown and stop before another order can be submitted.
            unknown = apply_result(
                order,
                OrderResult(
                    status="unknown",
                    accepted=False,
                    message="The executor returned no final order result.",
                ),
            )
            await orders_service.update_order(db, record, unknown)
            report.unknown += 1
            break

    return report
