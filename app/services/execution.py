"""Order submission: the bridge from an approved proposal to a venue.

This module is the only place that calls an :class:`OrderExecutor`. It follows a
fixed, safe order of work for every item in the plan:

1. submit sells before buys, always in the same deterministic order the paper
   preview used, so sale proceeds can fund the buys;
2. save the order (``prepared``) before anything else, keyed for idempotency;
3. call the executor once and record its answer;
4. for a **paper** fill, update the paper ledger with a new snapshot. A **live**
   result never touches the paper ledger: it is the preview, and the broker's
   own portfolio is what changes;
5. write one event for each step.

Which venue runs is decided by the proposal's execution mode:

* ``PAPER`` uses :class:`~app.infrastructure.simulation.PaperOrderExecutor`;
* ``LIVE`` uses :class:`~app.infrastructure.iol.order_executor.IOLOrderExecutor`
  after a fail-closed preflight. It is reached only through a recorded human
  approval, and a missing settlement term or validity raises
  :class:`ExecutionError` instead of quietly doing something else.

An order that already exists is never sent again, including a ``prepared`` row:
a crash may have happened after submission but before its result was saved. The
existing row is left for reconciliation rather than risking a duplicate. An
``unknown`` result is reported so the caller stops rather than guessing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain import brokers as broker_kinds
from app.domain.money import Money, money, quantity
from app.domain.trading import (
    ExecutionMode,
    Order,
    OrderResult,
    PaperLedger,
    Proposal,
    Recommendation,
)
from app.infrastructure.iol.order_executor import (
    IOLExecutionConfigError,
    IOLOrderExecutor,
    broker_status_of,
    live_validity_date,
)
from app.infrastructure.simulation import PaperOrderExecutor
from app.models import (
    EVENT_LEDGER,
    EVENT_ORDER,
    BrokerConnection,
    OrderRecord,
    ProposalRecord,
)
from app.ports import OrderExecutor
from app.services import broker as broker_service
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
    accepted: int = 0
    rejected: int = 0
    unknown: int = 0
    skipped: int = 0
    #: Symbols left unsubmitted because the actual cash could not fund them.
    deferred: list[str] = field(default_factory=list)
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
        market=recommendation.market,
        quotation_factor=recommendation.quotation_factor,
        status="prepared",
        execution_mode=execution_mode,
        created_at=datetime.now(UTC),
    )


def execution_order(orders: list[Order]) -> list[Order]:
    """Sells before buys, each group by symbol: the one deterministic order.

    The paper preview applied its fills in exactly this order, so the simulated
    portfolio and the real submission cannot disagree about sequencing.
    """
    rank = {"SELL": 0, "BUY": 1}
    return sorted(orders, key=lambda order: (rank.get(order.side, 2), order.symbol))


def funded_orders(orders: list[Order], available) -> tuple[list[Order], list[str]]:
    """Keep the buys the actual cash can pay for, in execution order.

    Returns the funded orders and the symbols deferred. Nothing here borrows
    against a sale that has not settled: only the cash handed in counts.
    """
    remaining = available.amount
    funded: list[Order] = []
    deferred: list[str] = []
    for order in orders:
        if order.notional.amount <= remaining:
            funded.append(order)
            remaining -= order.notional.amount
        else:
            deferred.append(order.symbol)
    return funded, deferred


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


async def _load_paper_ledger(
    db: AsyncSession, *, user_id: int, connection: BrokerConnection, country: str
) -> PaperLedger | None:
    """The ledger a paper fill is applied to.

    A paper account has its own saved state. For any other connection the
    ledger is the analysis preview, as before.
    """
    if broker_kinds.is_simulated(connection.broker):
        return await ledger_service.load_paper_account(
            db, user_id=user_id, connection=connection, country=country
        )
    return await ledger_service.load_ledger(
        db, user_id=user_id, connection=connection, country=country
    )


async def _save_paper_ledger(
    db: AsyncSession, *, user_id: int, connection: BrokerConnection, ledger: PaperLedger
) -> PaperLedger:
    if broker_kinds.is_simulated(connection.broker):
        return await ledger_service.save_paper_account(
            db, user_id=user_id, connection=connection, ledger=ledger
        )
    return await ledger_service.save_ledger(
        db, user_id=user_id, connection=connection, ledger=ledger
    )


async def _require_sales_first(
    db: AsyncSession, *, user_id: int, proposal: Proposal, proposal_id: int
) -> None:
    """Refuse a purchase while a planned sale has not been submitted.

    The plan is self-financing only if sales go first, so a purchase is never
    sent until every sale the plan lists has an order that is not a proven
    failure. A rejected or cancelled sale counts as not sent.
    """
    sent = {
        record.symbol
        for record in await orders_service.list_orders_for_proposal(
            db, user_id=user_id, proposal_id=proposal_id
        )
        if record.side == "SELL" and record.status not in orders_service.FAILED_STATES
    }
    unsent = sorted(
        item.symbol for item in proposal.orders if item.action == "SELL" and item.symbol not in sent
    )
    if unsent:
        raise ExecutionError(
            "Send the plan's sales before any purchase; not submitted yet: "
            + ", ".join(unsent)
            + "."
        )


def live_preflight(
    *,
    proposal: Proposal,
    orders: list[Order],
    ledger: PaperLedger | None,
    cash_limit: Money | None = None,
) -> None:
    """Refuse to build live orders unless everything needed is present.

    Fail-closed by design: a missing settlement parameter, an unknown market, a
    sell larger than the account held at review time, or a buy that the
    available cash cannot fund stops the run before a single POST. When
    ``cash_limit`` is given it is the cash the broker actually reports, and it
    is what buys are measured against.
    """
    settings = get_settings()
    problems: list[str] = []
    hint = (
        "set it in .env and recreate the container "
        "(docker compose up -d app), which is when the environment is read"
    )
    if not settings.iol_live_plazo.strip():
        problems.append(f"IOL_LIVE_PLAZO is not configured — {hint}")
    if not settings.iol_live_validity_days:
        problems.append(f"IOL_LIVE_VALIDITY_DAYS is not configured — {hint}")

    available = (
        cash_limit.amount
        if cash_limit is not None
        else (ledger.cash.amount if ledger is not None else Decimal("0"))
    )
    # Imported lazily so the execution service has no import-time cycle and
    # reads the same universe the plan was sized against.
    from app.services.analysis import load_config

    universe = load_config()
    for order in execution_order(orders):
        if not (order.market or "").strip():
            problems.append(f"{order.symbol}: no market is known for this instrument")
        if order.quantity <= 0 or order.quantity != order.quantity.to_integral_value():
            problems.append(f"{order.symbol}: live orders need whole units")
        if not order.limit_price.is_positive:
            problems.append(f"{order.symbol}: live orders need a positive limit price")

        instrument = universe.find(order.symbol)
        if instrument is None:
            problems.append(
                f"{order.symbol}: not in the configured universe, so its live "
                "quantity and quotation conventions are unverified"
            )
        else:
            if not instrument.live_quantity_confirmed:
                problems.append(
                    f"{order.symbol}: the meaning of IOL's 'cantidad' field for "
                    f"{instrument.category} is unverified, so live orders are "
                    "blocked. Verify it, then set live_quantity_confirmed = true "
                    f"in [universe.{instrument.category}] of config/universe.toml."
                )
            if order.quotation_factor != instrument.quotation_factor:
                problems.append(
                    f"{order.symbol}: the quotation factor changed since the plan "
                    f"was sized ({order.quotation_factor} vs "
                    f"{instrument.quotation_factor}); run a new analysis"
                )
            if instrument.min_quantity and order.quantity < instrument.min_quantity:
                problems.append(
                    f"{order.symbol}: {order.quantity} is below the minimum "
                    f"tradable quantity {instrument.min_quantity}"
                )

        if order.side == "SELL":
            held = ledger.position_for(order.symbol) if ledger else None
            if held is None or order.quantity > held.quantity:
                problems.append(
                    f"{order.symbol}: cannot sell {order.quantity} "
                    "when the reviewed portfolio held less"
                )
            else:
                available += order.notional.amount
        else:
            if order.notional.amount > available:
                problems.append(
                    f"{order.symbol}: the reviewed cash cannot fund "
                    f"{order.notional} without new capital"
                )
            else:
                available -= order.notional.amount

    if problems:
        raise ExecutionError("Live preflight refused to send orders: " + "; ".join(problems))
    if proposal.execution_mode != "LIVE":
        raise ExecutionError(
            f"Proposal execution mode is {proposal.execution_mode}; "
            "the live executor only runs LIVE proposals."
        )


async def _live_executor(connection: BrokerConnection) -> tuple[OrderExecutor, Any]:
    """A ready IOL executor and its client: authenticated, with trading terms.

    The caller owns the client and closes it when the submission pass ends.
    """
    settings = get_settings()
    try:
        client, access_token, renew = await broker_service.trading_session(connection)
        executor = IOLOrderExecutor(
            client=client,
            access_token=access_token,
            plazo=settings.iol_live_plazo,
            validity=live_validity_date(settings.iol_live_validity_days),
            renew_token=renew,
        )
        return executor, client
    except IOLExecutionConfigError as exc:
        raise ExecutionError(str(exc)) from exc


async def submit_proposal_orders(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    proposal_record: ProposalRecord,
    executor: OrderExecutor | None = None,
    side: str | None = None,
    cash_limit: Money | None = None,
) -> ExecutionReport:
    """Submit one phase of a proposal's orders exactly once.

    ``side`` selects the phase: ``"SELL"`` for the sales pass, ``"BUY"`` for
    the purchases pass, ``None`` for everything. Buys are limited to what
    ``cash_limit`` (the cash the broker actually reports) can pay for; the
    rest is deferred, never borrowed against unsettled sales.
    """
    proposal = Proposal.model_validate(proposal_record.payload)
    if proposal_record.user_id != user_id or proposal_record.connection_id != connection.id:
        raise ExecutionError("The proposal does not belong to this connection.")
    if side == "BUY":
        await _require_sales_first(
            db, user_id=user_id, proposal=proposal, proposal_id=proposal_record.id
        )

    ledger = await _load_paper_ledger(
        db, user_id=user_id, connection=connection, country=proposal.country
    )
    orders = execution_order(
        [
            order_from_recommendation(item, proposal.execution_mode)
            for item in proposal.orders
            if side is None or item.action == side
        ]
    )

    report = ExecutionReport()
    if side == "BUY" and cash_limit is not None:
        orders, report.deferred = funded_orders(orders, cash_limit)
        for symbol in report.deferred:
            logger.info("buy %s deferred: not covered by the available cash", symbol)

    live = proposal.execution_mode == "LIVE"
    if not live and proposal.execution_mode != "PAPER":
        raise ExecutionError(f"Unknown execution mode {proposal.execution_mode!r}.")
    if not live and ledger is None:
        raise ExecutionError("No paper ledger is available for this proposal.")

    live_client: Any = None
    if executor is None:
        if live:
            reviewed_ledger = proposal.ledger_before or ledger
            live_preflight(
                proposal=proposal,
                orders=orders,
                ledger=reviewed_ledger,
                cash_limit=cash_limit,
            )
            executor, live_client = await _live_executor(connection)
        else:
            executor = PaperOrderExecutor()

    try:
        for order in orders:
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
                    f"Prepared {order.side} {order.quantity} {order.symbol} at {order.limit_price}."
                ),
                proposal_id=proposal_record.id,
                order_id=record.id,
                mode=proposal.execution_mode,
            )

            await events_service.record_event(
                db,
                user_id=user_id,
                connection_id=connection.id,
                category=EVENT_ORDER,
                event_type="order.submitted",
                message=(f"{order.side} {order.quantity} {order.symbol} sent to the executor."),
                proposal_id=proposal_record.id,
                order_id=record.id,
                mode=proposal.execution_mode,
            )

            result = await executor.submit(
                order.model_copy(update={"external_id": record.idempotency_key})
            )
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

            if result.status == "accepted":
                # The broker created the operation. That is not a fill, and the
                # paper ledger is a preview: a live result never touches it.
                report.accepted += 1
                continue

            if result.is_fill:
                if live:
                    # Defence in depth: a live fill must never become a paper fill.
                    report.filled += 1
                    continue
                if ledger is None:
                    raise ExecutionError("No paper ledger is available for this proposal.")
                ledger = ledger_service.apply_fill(
                    ledger,
                    order,
                    filled_quantity=result.filled_quantity,
                    filled_price=result.filled_price or order.limit_price,
                )
                await _save_paper_ledger(db, user_id=user_id, connection=connection, ledger=ledger)
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

    finally:
        if live_client is not None:
            await live_client.aclose()

    return report


async def execute_single_order(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    proposal_record: ProposalRecord,
    symbol: str,
    side: str,
    units: Decimal,
    executor: OrderExecutor | None = None,
) -> ExecutionReport:
    """Send one reviewed trade, sized by the user, exactly once per attempt.

    ``units`` is the amount the person chose (the plan's suggestion is only a
    starting point); the limit price and the instrument metadata come from the
    reviewed plan. The same trade may be sent again **only** after a proven
    failure (rejected or cancelled): an order the broker may still act on, or
    whose outcome is unknown, blocks every further attempt for that trade.
    """
    proposal = Proposal.model_validate(proposal_record.payload)
    if proposal_record.user_id != user_id or proposal_record.connection_id != connection.id:
        raise ExecutionError("The proposal does not belong to this connection.")
    if side not in ("BUY", "SELL"):
        raise ExecutionError(f"Unknown side {side!r}.")

    planned = next(
        (item for item in proposal.orders if item.symbol == symbol and item.action == side),
        None,
    )
    if planned is None or planned.limit_price is None:
        raise ExecutionError(f"{symbol} {side} is not one of this plan's trades.")

    if units <= 0 or units != units.to_integral_value():
        raise ExecutionError("Quantity must be a whole number of units.")
    if units > planned.quantity:
        raise ExecutionError(
            f"{symbol} {side}: {units} units is more than the {planned.quantity} the reviewed "
            "plan allows. Send at most the planned quantity, or run a new analysis."
        )
    if side == "BUY":
        await _require_sales_first(
            db, user_id=user_id, proposal=proposal, proposal_id=proposal_record.id
        )
    attempt, allowed, reason = await orders_service.next_attempt(
        db,
        user_id=user_id,
        proposal_id=proposal_record.id,
        symbol=symbol,
        side=side,
    )
    if not allowed:
        raise ExecutionError(f"{symbol} {side} cannot be sent again: {reason}.")

    factor = planned.quotation_factor or 1
    notional = money(planned.limit_price.amount * units / factor)
    order = Order(
        symbol=symbol,
        side=side,
        quantity=units,
        limit_price=planned.limit_price,
        notional=notional,
        market=planned.market,
        quotation_factor=factor,
        status="prepared",
        execution_mode=proposal.execution_mode,
        created_at=datetime.now(UTC),
    )

    live = proposal.execution_mode == "LIVE"
    if not live and proposal.execution_mode != "PAPER":
        raise ExecutionError(f"Unknown execution mode {proposal.execution_mode!r}.")

    ledger = await _load_paper_ledger(
        db, user_id=user_id, connection=connection, country=proposal.country
    )
    cash_limit = None
    if side == "BUY" and live:
        # The cash a purchase may spend is what the broker reports now, not what
        # an earlier read saw: trades since then may have used it.
        refreshed = await broker_service.read_account_status(
            db, user_id=user_id, connection=connection
        )
        if not refreshed.ok:
            raise ExecutionError(
                "The cash the broker reports could not be confirmed right now, so "
                "no purchase was sent."
            )
        cash_limit = await broker_service.available_cash(
            db, user_id=user_id, connection=connection, currency=proposal.currency
        )
        if cash_limit is None:
            raise ExecutionError(
                "The broker reports no available cash, so no purchase can be funded."
            )

    live_client: Any = None
    if executor is None:
        if live:
            live_preflight(
                proposal=proposal,
                orders=[order],
                ledger=proposal.ledger_before or ledger,
                cash_limit=cash_limit,
            )
            executor, live_client = await _live_executor(connection)
        else:
            executor = PaperOrderExecutor()

    report = ExecutionReport()
    try:
        record, created = await orders_service.prepare_order(
            db,
            user_id=user_id,
            connection_id=connection.id,
            proposal_id=proposal_record.id,
            country=proposal.country,
            order=order,
            attempt=attempt,
        )
        report.records.append(record)
        if not created:
            report.skipped += 1
            return report

        await events_service.record_event(
            db,
            user_id=user_id,
            connection_id=connection.id,
            category=EVENT_ORDER,
            event_type="order.prepared",
            message=(
                f"Prepared {order.side} {order.quantity} {order.symbol} at {order.limit_price}."
            ),
            proposal_id=proposal_record.id,
            order_id=record.id,
            mode=proposal.execution_mode,
        )
        await events_service.record_event(
            db,
            user_id=user_id,
            connection_id=connection.id,
            category=EVENT_ORDER,
            event_type="order.submitted",
            message=(f"{order.side} {order.quantity} {order.symbol} sent to the executor."),
            proposal_id=proposal_record.id,
            order_id=record.id,
            mode=proposal.execution_mode,
        )

        result = await executor.submit(
            order.model_copy(update={"external_id": record.idempotency_key})
        )
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
        elif result.status == "accepted":
            report.accepted += 1
        elif result.is_fill:
            report.filled += 1
            if not live:
                # Paper fills move the simulated ledger; a live result never does.
                if ledger is None:
                    raise ExecutionError("No paper ledger is available for this proposal.")
                ledger = ledger_service.apply_fill(
                    ledger,
                    order,
                    filled_quantity=result.filled_quantity,
                    filled_price=result.filled_price or order.limit_price,
                )
                await _save_paper_ledger(db, user_id=user_id, connection=connection, ledger=ledger)
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
        elif result.status == "rejected":
            report.rejected += 1
        else:
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
    finally:
        if live_client is not None:
            await live_client.aclose()

    return report


async def reconcile_live_order(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    record: OrderRecord,
) -> OrderRecord:
    """One live order against the broker's own operation state.

    Reads ``GET /api/v2/operaciones/{numero}`` and records only what the
    broker proves: the actual executed quantity and the actual execution
    price. The requested limit price is never reported as an execution price,
    a partial fill is never shown as a complete fill, and nothing here ever
    resends an order.
    """
    order = orders_service.order_from_record(record)
    if not order.broker_order_id:
        raise ExecutionError("This order has no broker operation number to reconcile.")
    if order.execution_mode != "LIVE":
        raise ExecutionError("Only live orders can be reconciled with the broker.")

    client, access_token, _renew = await broker_service.trading_session(connection)
    try:
        operation = await client.get_operation(access_token, order.broker_order_id)
    finally:
        await client.aclose()
    state = broker_status_of(operation, quantity=order.quantity)

    filled_quantity = (
        quantity(state.filled_quantity)
        if state.filled_quantity is not None
        else order.filled_quantity
    )
    updates: dict[str, object] = {"message": state.message}
    if state.status and not order.is_settled:
        updates["status"] = state.status
    if state.status in {"filled", "partially_filled"}:
        updates["filled_quantity"] = filled_quantity
        if state.filled_price is not None:
            updates["filled_price"] = state.filled_price
            updates["filled_notional"] = money(state.filled_price.amount * (filled_quantity or 0))
    updated = order.model_copy(update=updates)
    return await orders_service.update_order(db, record, updated)


async def reconcile_proposal_orders(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    proposal_record: ProposalRecord,
) -> dict[str, int]:
    """Refresh every submitted live order of one proposal from the broker.

    Safe to press repeatedly: each order is read, never sent. Returns how many
    orders were filled, partially filled, cancelled, rejected, or left pending.
    """
    counts = {
        "filled": 0,
        "partial": 0,
        "cancelled": 0,
        "rejected": 0,
        "pending": 0,
        "failed": 0,
        "unresolved": 0,
    }
    records = await orders_service.list_orders_for_proposal(
        db, user_id=user_id, proposal_id=proposal_record.id
    )
    for record in records:
        if not record.broker_order_id and record.status in {"unknown", "prepared", "submitted"}:
            # No operation number came back, so IOL cannot be asked about it. It
            # stays blocked from being resent and has to be checked at IOL.
            counts["unresolved"] += 1
            continue
        if record.status not in {"accepted", "submitted", "partially_filled"}:
            continue
        if not record.broker_order_id:
            continue
        before = record.status
        try:
            record = await reconcile_live_order(
                db, user_id=user_id, connection=connection, record=record
            )
        except Exception as exc:  # noqa: BLE001 - one unreadable order must not block the rest
            logger.warning("reconcile order id=%s failed: %s", record.id, exc)
            counts["failed"] += 1
            continue
        after = record.status
        if after == "filled":
            counts["filled"] += 1
        elif after == "partially_filled":
            counts["partial"] += 1
        elif after == "cancelled":
            counts["cancelled"] += 1
        elif after == "rejected":
            counts["rejected"] += 1
        else:
            counts["pending"] += 1
        if after != before:
            await events_service.record_event(
                db,
                user_id=user_id,
                connection_id=connection.id,
                category=EVENT_ORDER,
                event_type=f"order.{after}",
                message=(
                    f"{record.symbol}: order {record.id} reconciled with IOL ({before} -> {after})."
                ),
                proposal_id=proposal_record.id,
                order_id=record.id,
                mode="LIVE",
            )
    return counts
