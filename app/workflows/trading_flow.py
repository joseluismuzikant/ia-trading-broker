"""The Day 5 trading graph: plan, human approval, fresh check, paper submit.

The graph is built with LangGraph. It is deliberately re-entrant: PostgreSQL is
the source of truth for the approval, the order, and the history, so the graph
can be invoked again after a restart and it will re-derive its position from the
database instead of from an in-memory checkpoint. The human pause is a run that
stops at the approval step; the continuation is the same graph invoked again,
now that the approval row says ``approved``.

Every step is idempotent:

* the plan step loads the saved proposal when there is one instead of saving a
  second plan;
* the approval step reuses the open approval instead of creating a new one;
* the submit step finds orders by their idempotency key and never sends one a
  second time.

No node places a live order. Only the paper executor is wired in, and live
trading stays off.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.money import Money
from app.domain.trading import (
    MAX_CASH_DRIFT,
    MAX_PRICE_DRIFT,
    PaperLedger,
    Proposal,
    RiskCheck,
)
from app.infrastructure.iol.schemas import Quote
from app.models import (
    APPROVAL_APPROVED,
    APPROVAL_CONSUMED,
    APPROVAL_PENDING,
    EVENT_APPROVAL,
    EVENT_PROPOSAL,
    EVENT_RUN,
    BrokerConnection,
)
from app.services import analysis as analysis_service
from app.services import approvals as approvals_service
from app.services import connections as connections_service
from app.services import events as events_service
from app.services import execution as execution_service
from app.services import ledger as ledger_service

logger = logging.getLogger("ia_trading_broker.workflow")


class WorkflowError(Exception):
    """The workflow could not be built or run."""


#: Run states that the pages and the tests read.
STATUS_RUNNING = "running"
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_COMPLETED = "completed"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"
STATUS_BLOCKED = "blocked"
STATUS_NO_TRADE = "no_trade"
STATUS_FAILED = "failed"
STATUS_UNSUPPORTED = "unsupported"


class TradingState(TypedDict, total=False):
    """The graph state. Plain data only, so it is trivially serialisable."""

    proposal_id: int | None
    approval_id: int | None
    status: str | None
    current_node: str
    paused: bool
    has_orders: bool
    approval_status: str | None
    message: str | None


def _drift(reference: Decimal, current: Decimal) -> Decimal:
    """Relative change from ``reference`` to ``current``."""
    if reference == 0:
        return Decimal("0") if current == 0 else Decimal("1")
    return abs(current - reference) / abs(reference)


def _latest_price(
    symbol: str, history: dict[str, list[Quote]], ledger: PaperLedger
) -> Money | None:
    """The freshest saved price for a symbol, from history or the ledger."""
    quotes = history.get(symbol) or []
    closes = [quote.ultimo_precio for quote in quotes if quote.ultimo_precio]
    if closes:
        ordered = sorted(quotes, key=lambda quote: quote.fecha_hora or "")
        priced = [quote.ultimo_precio for quote in ordered if quote.ultimo_precio]
        if priced:
            return Money(Decimal(str(priced[-1])))
    position = ledger.position_for(symbol)
    return position.last_price if position is not None else None


def fresh_data_checks(
    proposal: Proposal,
    ledger: PaperLedger | None,
    history: dict[str, list[Quote]],
) -> list[RiskCheck]:
    """Compare a proposal against the latest saved data.

    A large move in the cash or in a traded price stops the plan, so an approval
    never releases an order into a market that has changed since the review.
    """
    if ledger is None:
        return [
            RiskCheck(
                name="fresh_ledger",
                passed=False,
                detail="No paper ledger is available to check against.",
            )
        ]

    cash_drift = _drift(proposal.cash.amount, ledger.cash.amount)
    checks = [
        RiskCheck(
            name="fresh_cash",
            passed=cash_drift <= MAX_CASH_DRIFT,
            detail=(
                f"Paper cash moved {float(cash_drift):.2%}, "
                f"within the {float(MAX_CASH_DRIFT):.0%} limit."
                if cash_drift <= MAX_CASH_DRIFT
                else (
                    f"Paper cash moved {float(cash_drift):.2%}, "
                    f"over the {float(MAX_CASH_DRIFT):.0%} limit."
                )
            ),
        )
    ]

    for item in proposal.orders:
        reference = item.limit_price or item.evidence.last_price
        current = _latest_price(item.symbol, history, ledger)
        if reference is None or current is None:
            checks.append(
                RiskCheck(
                    name=f"fresh_price_{item.symbol}",
                    passed=False,
                    detail=f"No fresh price is available for {item.symbol}.",
                )
            )
            continue
        drift = _drift(reference.amount, current.amount)
        checks.append(
            RiskCheck(
                name=f"fresh_price_{item.symbol}",
                passed=drift <= MAX_PRICE_DRIFT,
                detail=(
                    f"{item.symbol} moved {float(drift):.2%}, "
                    f"within the {float(MAX_PRICE_DRIFT):.0%} limit."
                    if drift <= MAX_PRICE_DRIFT
                    else (
                        f"{item.symbol} moved {float(drift):.2%}, "
                        f"over the {float(MAX_PRICE_DRIFT):.0%} limit."
                    )
                ),
            )
        )
    return checks


def build_graph(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    country: str,
):
    """Compile the trading graph for one connection and country.

    The session and the connection are captured in the closures, so the graph
    state holds only plain data and never an ORM object.
    """

    async def _record(
        category: str, event_type: str, message: str, **extra: object
    ) -> None:
        await events_service.record_event(
            db,
            user_id=user_id,
            connection_id=connection.id,
            category=category,
            event_type=event_type,
            message=message,
            **extra,  # type: ignore[arg-type]
        )

    async def plan(state: TradingState) -> TradingState:
        """Create the proposal once, or load the one already saved."""
        proposal_id = state.get("proposal_id")
        if proposal_id:
            record = await analysis_service.get_proposal(
                db, user_id=user_id, proposal_id=proposal_id
            )
            if record is None:
                return {
                    "status": STATUS_FAILED,
                    "message": "The proposal to resume no longer exists.",
                    "current_node": "plan",
                }
            proposal = Proposal.model_validate(record.payload)
            # A proposal that was already decided must not open a new approval.
            approval = await approvals_service.latest_approval(
                db, user_id=user_id, proposal_id=proposal_id
            )
            if record.status == "approved" or (
                approval is not None and approval.status == APPROVAL_CONSUMED
            ):
                return {
                    "current_node": "plan",
                    "status": STATUS_COMPLETED,
                    "message": "This proposal was already executed.",
                }
            if record.status == "rejected":
                return {
                    "current_node": "plan",
                    "status": STATUS_REJECTED,
                    "message": "This proposal was rejected.",
                }
            if record.status == "expired":
                return {
                    "current_node": "plan",
                    "status": STATUS_EXPIRED,
                    "message": "This proposal expired.",
                }
            return {
                "current_node": "plan",
                "has_orders": bool(proposal.orders),
            }

        ledger = await ledger_service.load_ledger(
            db, user_id=user_id, connection=connection, country=country
        )
        if ledger is None:
            return {
                "status": STATUS_FAILED,
                "message": (
                    "No paper ledger exists for this country yet. Create one "
                    "from the portfolio before running an analysis."
                ),
                "current_node": "plan",
            }

        # The funnel and the policy run on the snapshots the analysis step just
        # saved, so planning costs no broker calls and a resumed run replays
        # exactly the same inputs rather than a newer market.
        proposal = await analysis_service.build_plan(
            db, user_id=user_id, connection=connection, ledger=ledger
        )
        record = await analysis_service.save_proposal(
            db, user_id=user_id, connection_id=connection.id, proposal=proposal
        )
        await _record(
            EVENT_PROPOSAL,
            "proposal.created",
            f"Proposal {record.id} saved with {len(proposal.orders)} order(s) "
            f"from {len(proposal.finalists)} finalists.",
            proposal_id=record.id,
            mode=proposal.approval_mode,
        )
        return {
            "current_node": "plan",
            "proposal_id": record.id,
            "has_orders": bool(proposal.orders),
        }

    def route_after_plan(state: TradingState) -> str:
        status = state.get("status")
        if status == STATUS_FAILED:
            return "failed"
        if status == STATUS_COMPLETED:
            return "already_executed"
        if status == STATUS_REJECTED:
            return "rejected"
        if status == STATUS_EXPIRED:
            return "expired"
        if not state.get("has_orders"):
            return "no_trade"
        return "request_approval"

    async def request_approval(state: TradingState) -> TradingState:
        """Open the human approval exactly once."""
        record = await analysis_service.get_proposal(
            db, user_id=user_id, proposal_id=state["proposal_id"]
        )
        assert record is not None
        proposal = Proposal.model_validate(record.payload)

        if proposal.approval_mode != "HUMAN_IN_THE_LOOP":
            return {
                "status": STATUS_UNSUPPORTED,
                "message": (
                    f"Approval mode {proposal.approval_mode} is not enabled yet. "
                    "Only human approval runs today."
                ),
                "current_node": "request_approval",
            }

        # Reuse whatever approval the proposal already has, in any state. Only a
        # proposal with no approval at all opens a new one, so a rejected or
        # expired decision is never reopened by a restart.
        existing = await approvals_service.latest_approval(
            db, user_id=user_id, proposal_id=record.id
        )
        if existing is None:
            approval = await approvals_service.request_approval(
                db, user_id=user_id, connection_id=connection.id, proposal_id=record.id
            )
            await _record(
                EVENT_APPROVAL,
                "approval.requested",
                f"Approval requested for proposal {record.id}; it expires in "
                f"{approvals_service.APPROVAL_TTL_HOURS} hours.",
                proposal_id=record.id,
                mode=proposal.approval_mode,
            )
        else:
            approval = existing
        return {
            "current_node": "request_approval",
            "approval_id": approval.id,
        }

    async def await_approval(state: TradingState) -> TradingState:
        """Stop while the approval is pending; continue once it is approved."""
        approval = await approvals_service.get_approval(
            db, user_id=user_id, approval_id=state["approval_id"]
        )
        if approval is None:
            return {
                "status": STATUS_FAILED,
                "message": "The approval for this proposal disappeared.",
                "current_node": "await_approval",
            }
        approval = await approvals_service.refresh_status(db, approval)

        if approval.status == APPROVAL_APPROVED:
            return {"current_node": "await_approval", "approval_status": APPROVAL_APPROVED}
        if approval.status == APPROVAL_CONSUMED:
            return {
                "current_node": "await_approval",
                "approval_status": APPROVAL_CONSUMED,
                "status": STATUS_COMPLETED,
                "message": "This proposal was already executed.",
            }
        if approval.status == "rejected":
            return {
                "status": STATUS_REJECTED,
                "message": approval.reason or "Rejected by the user.",
                "current_node": "await_approval",
            }
        if approval.status == "expired":
            return {
                "status": STATUS_EXPIRED,
                "message": "The approval window expired before a decision.",
                "current_node": "await_approval",
            }
        return {
            "status": STATUS_AWAITING_APPROVAL,
            "approval_status": APPROVAL_PENDING,
            "paused": True,
            "current_node": "await_approval",
        }

    def route_after_approval(state: TradingState) -> str:
        approval_status = state.get("approval_status")
        if approval_status == APPROVAL_APPROVED:
            return "recheck"
        if approval_status == APPROVAL_CONSUMED or state.get("status") == STATUS_COMPLETED:
            return "already_executed"
        if state.get("status") == STATUS_REJECTED:
            return "rejected"
        if state.get("status") == STATUS_EXPIRED:
            return "expired"
        return "paused"

    async def recheck(state: TradingState) -> TradingState:
        """Re-read the saved data and refuse to submit a stale plan."""
        record = await analysis_service.get_proposal(
            db, user_id=user_id, proposal_id=state["proposal_id"]
        )
        assert record is not None
        proposal = Proposal.model_validate(record.payload)
        ledger = await ledger_service.load_ledger(
            db, user_id=user_id, connection=connection, country=country
        )
        symbols = {item.symbol for item in proposal.orders}
        history = await analysis_service.load_history(
            db, user_id=user_id, connection=connection, symbols=symbols
        )
        checks = fresh_data_checks(proposal, ledger, history)
        failed = [check for check in checks if not check.passed]
        if failed:
            detail = failed[0].detail
            await _record(
                EVENT_RUN,
                "run.blocked",
                f"Fresh-data check stopped the plan: {detail}",
                proposal_id=record.id,
                mode=proposal.execution_mode,
                payload={"checks": [check.model_dump() for check in checks]},
            )
            return {
                "status": STATUS_BLOCKED,
                "message": f"Fresh-data check stopped the plan: {detail}",
                "current_node": "recheck",
            }
        return {"current_node": "recheck"}

    def route_after_recheck(state: TradingState) -> str:
        return "blocked" if state.get("status") == STATUS_BLOCKED else "submit"

    async def submit(state: TradingState) -> TradingState:
        """Consume the approval and submit the orders to the paper executor."""
        record = await analysis_service.get_proposal(
            db, user_id=user_id, proposal_id=state["proposal_id"]
        )
        assert record is not None
        proposal = Proposal.model_validate(record.payload)

        try:
            report = await execution_service.submit_proposal_orders(
                db, user_id=user_id, connection=connection, proposal_record=record
            )
        except (execution_service.ExecutionError, ledger_service.LedgerError) as exc:
            message = f"Paper submission was blocked: {exc}"
            await _record(
                EVENT_RUN,
                "run.blocked",
                message,
                proposal_id=record.id,
                mode=proposal.execution_mode,
            )
            return {
                "status": STATUS_BLOCKED,
                "message": message,
                "current_node": "submit",
            }

        if report.needs_reconciliation or report.rejected:
            message = (
                "Paper submission did not complete: "
                f"{report.rejected} rejected, {report.unknown} unknown."
            )
            await _record(
                EVENT_RUN,
                "run.blocked",
                message,
                proposal_id=record.id,
                mode=proposal.execution_mode,
            )
            return {
                "status": STATUS_BLOCKED,
                "message": message,
                "current_node": "submit",
            }

        approval = await approvals_service.latest_approval(
            db, user_id=user_id, proposal_id=record.id
        )
        if approval is not None and approval.status == APPROVAL_APPROVED:
            await approvals_service.consume(db, approval)

        record.status = "approved"
        await db.commit()

        message = (
            f"Paper submission finished: {report.filled} filled, "
            f"{report.skipped} already handled."
        )
        await _record(
            EVENT_RUN,
            "run.completed",
            message,
            proposal_id=record.id,
            mode=proposal.execution_mode,
            payload={
                "filled": report.filled,
                "rejected": report.rejected,
                "unknown": report.unknown,
                "skipped": report.skipped,
            },
        )
        return {
            "status": STATUS_COMPLETED,
            "message": message,
            "current_node": "submit",
        }

    async def no_trade(state: TradingState) -> TradingState:
        await _record(
            EVENT_RUN,
            "run.no_trade",
            "The analysis produced no orders, so there is nothing to approve.",
            proposal_id=state.get("proposal_id"),
        )
        return {
            "status": STATUS_NO_TRADE,
            "message": "No trade was proposed.",
            "current_node": "no_trade",
        }

    async def rejected(state: TradingState) -> TradingState:
        await _finalise_proposal(state, "rejected")
        await _record(
            EVENT_APPROVAL,
            "approval.rejected",
            "The proposal was rejected; no order was sent.",
            proposal_id=state.get("proposal_id"),
        )
        return {"current_node": "rejected"}

    async def expired(state: TradingState) -> TradingState:
        await _finalise_proposal(state, "expired")
        await _record(
            EVENT_APPROVAL,
            "approval.expired",
            "The approval expired; no order was sent.",
            proposal_id=state.get("proposal_id"),
        )
        return {"current_node": "expired"}

    async def blocked(state: TradingState) -> TradingState:
        # The recheck node already recorded the reason and the checks.
        return {"current_node": "blocked"}

    async def failed(state: TradingState) -> TradingState:
        await _record(
            EVENT_RUN,
            "run.failed",
            state.get("message") or "The run failed.",
            proposal_id=state.get("proposal_id"),
        )
        return {"current_node": "failed"}

    async def unsupported(state: TradingState) -> TradingState:
        await _record(
            EVENT_RUN,
            "run.unsupported",
            state.get("message") or "This approval mode is not enabled.",
            proposal_id=state.get("proposal_id"),
        )
        return {"current_node": "unsupported"}

    async def already_executed(state: TradingState) -> TradingState:
        return {"current_node": "already_executed"}

    async def _finalise_proposal(state: TradingState, status: str) -> None:
        proposal_id = state.get("proposal_id")
        if not proposal_id:
            return
        record = await analysis_service.get_proposal(
            db, user_id=user_id, proposal_id=proposal_id
        )
        if record is not None:
            record.status = status
            await db.commit()

    graph = StateGraph(TradingState)
    graph.add_node("plan", plan)
    graph.add_node("request_approval", request_approval)
    graph.add_node("await_approval", await_approval)
    graph.add_node("recheck", recheck)
    graph.add_node("submit", submit)
    graph.add_node("no_trade", no_trade)
    graph.add_node("rejected", rejected)
    graph.add_node("expired", expired)
    graph.add_node("blocked", blocked)
    graph.add_node("failed", failed)
    graph.add_node("unsupported", unsupported)
    graph.add_node("already_executed", already_executed)

    graph.add_edge(START, "plan")
    graph.add_conditional_edges(
        "plan",
        route_after_plan,
        {
            "failed": "failed",
            "no_trade": "no_trade",
            "request_approval": "request_approval",
            "already_executed": "already_executed",
            "rejected": "rejected",
            "expired": "expired",
        },
    )
    graph.add_edge("request_approval", "await_approval")
    graph.add_conditional_edges(
        "await_approval",
        route_after_approval,
        {
            "recheck": "recheck",
            "rejected": "rejected",
            "expired": "expired",
            "already_executed": "already_executed",
            "paused": END,
        },
    )
    graph.add_conditional_edges(
        "recheck",
        route_after_recheck,
        {"submit": "submit", "blocked": "blocked"},
    )
    for terminal in (
        "submit",
        "no_trade",
        "rejected",
        "expired",
        "blocked",
        "failed",
        "unsupported",
        "already_executed",
    ):
        graph.add_edge(terminal, END)

    return graph.compile()


def _initial_state(
    *, proposal_id: int | None, approval_id: int | None
) -> dict[str, object]:
    return {
        "proposal_id": proposal_id,
        "approval_id": approval_id,
        "status": STATUS_RUNNING,
        "current_node": "start",
        "paused": False,
        "has_orders": False,
        "approval_status": None,
        "message": None,
    }


async def start_run(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    country: str,
) -> dict[str, object]:
    """Create a proposal and pause it for human approval (or finish if empty)."""
    graph = build_graph(db, user_id=user_id, connection=connection, country=country)
    state = _initial_state(proposal_id=None, approval_id=None)
    return await graph.ainvoke(state)


async def resume_run(
    db: AsyncSession, *, user_id: int, proposal_id: int
) -> dict[str, object]:
    """Continue a paused run after its approval was saved.

    Nothing in memory is required: the graph re-derives its position from the
    saved proposal and approval, so a restart changes nothing.
    """
    record = await analysis_service.get_proposal(
        db, user_id=user_id, proposal_id=proposal_id
    )
    if record is None:
        raise WorkflowError("Proposal not found.")
    connection = await connections_service.get_connection(
        db, user_id=user_id, connection_id=record.connection_id
    )
    approval = await approvals_service.latest_approval(
        db, user_id=user_id, proposal_id=proposal_id
    )
    graph = build_graph(
        db, user_id=user_id, connection=connection, country=record.country
    )
    state = _initial_state(
        proposal_id=proposal_id, approval_id=approval.id if approval else None
    )
    return await graph.ainvoke(state)
