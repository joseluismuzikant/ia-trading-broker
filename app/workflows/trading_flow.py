"""The MVP trading graph: fresh broker state, one model answer, human, live.

The graph is built with LangGraph and is deliberately re-entrant: PostgreSQL is
the source of truth for the proposal, the approval, and the orders, so a
restart resumes from the database instead of from an in-memory checkpoint. The
human pause is a run that stops at the approval step; the continuation is the
same graph invoked again, now that the approval row says ``approved``.

The flow is the product flow, one node per stage::

    load_broker_state -> create_paper_ledger -> build_analysis_context
        -> analyse_portfolio (the ONE AI call) -> validate_advice
        -> apply_to_paper_ledger -> request_approval -> await_approval
            -> rejected/expired/pending: end
            -> approved: execute_live -> end

There is exactly one AI node: the whole account in, one target allocation out.
No agents, no per-ticker calls. The model's allocation is the source of the
rebalance; deterministic Python turns it into exact trades.

Live submission happens only after a recorded human approval and a passing
fail-closed preflight; otherwise ``execute_live`` stops with a clear block
instead of quietly simulating anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.portfolio import Portfolio
from app.domain.trading import Proposal
from app.models import (
    APPROVAL_APPROVED,
    APPROVAL_CONSUMED,
    APPROVAL_PENDING,
    EVENT_APPROVAL,
    EVENT_PROPOSAL,
    EVENT_RUN,
    BrokerConnection,
)
from app.services import advice as advice_service
from app.services import analysis as analysis_service
from app.services import approvals as approvals_service
from app.services import broker as broker_service
from app.services import connections as connections_service
from app.services import events as events_service
from app.services import execution as execution_service
from app.services import ledger as ledger_service
from app.services import rebalance as rebalance_service
from app.tracing import run_metadata

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
STATUS_FAILED = "failed"


class TradingState(TypedDict, total=False):
    """The graph state.

    Control fields are plain data. The analysis intermediates are the objects
    one invocation passes between its own nodes; a resume re-reads everything
    it needs from the database.
    """

    proposal_id: int | None
    approval_id: int | None
    status: str | None
    paused: bool
    approval_status: str | None
    message: str | None
    #: Which execution phase this run submits: "sells" (default) or "buys".
    phase: str | None
    ledger_before: Any
    context: Any
    evidence: Any
    prices: Any
    warnings: Any
    model: Any
    advice: Any
    advice_view: Any


@dataclass
class Run:
    """The graph's nodes for one connection and country.

    The database session and the connection live here, never in the graph
    state, so the state holds no ORM object.
    """

    db: AsyncSession
    user_id: int
    connection: BrokerConnection
    country: str
    model_key: str | None = None

    async def record(self, category: str, event_type: str, message: str, **extra: object) -> None:
        await events_service.record_event(
            self.db,
            user_id=self.user_id,
            connection_id=self.connection.id,
            category=category,
            event_type=event_type,
            message=message,
            **extra,  # type: ignore[arg-type]
        )

    async def fail(self, state: TradingState, message: str) -> dict[str, object]:
        """The state that stops a run, with the reason kept in history."""
        await self.record(EVENT_RUN, "run.failed", message, proposal_id=state.get("proposal_id"))
        return {"status": STATUS_FAILED, "message": message}

    async def proposal(self, state: TradingState) -> tuple[Any, Proposal]:
        """The saved proposal this run is working on."""
        record = await analysis_service.get_proposal(
            self.db, user_id=self.user_id, proposal_id=state["proposal_id"]
        )
        assert record is not None
        return record, Proposal.model_validate(record.payload)

    async def finalise(self, state: TradingState, status: str) -> None:
        proposal_id = state.get("proposal_id")
        if not proposal_id:
            return
        record = await analysis_service.get_proposal(
            self.db, user_id=self.user_id, proposal_id=proposal_id
        )
        if record is not None:
            record.status = status
            await self.db.commit()

    # --- Analysis ---------------------------------------------------------

    async def load_broker_state(self, state: TradingState) -> dict[str, object]:
        """Refresh the account, the portfolio, and the daily history."""
        await self.record(
            EVENT_RUN,
            "analysis.started",
            f"Analysis started for {self.country}.",
            mode="LIVE",
        )
        warnings: list[str] = []

        account = await broker_service.read_account_status(
            self.db, user_id=self.user_id, connection=self.connection
        )
        if not account.ok:
            warnings.append(f"The account status could not be refreshed ({account.error}).")

        portfolio_read = await broker_service.read_portfolio(
            self.db, user_id=self.user_id, connection=self.connection, country=self.country
        )
        if portfolio_read.snapshot is None:
            return await self.fail(
                state,
                "No broker portfolio could be read for this country, so there "
                "is nothing to rebalance.",
            )
        if not portfolio_read.ok:
            warnings.append(
                f"The broker portfolio could not be refreshed "
                f"({portfolio_read.error}); the last saved portfolio is used."
            )

        portfolio = Portfolio.model_validate(portfolio_read.snapshot.payload)
        universe = analysis_service.load_config()
        baseline = ledger_service.ledger_from_portfolio(
            portfolio, source_snapshot_id=portfolio_read.snapshot.id, universe=universe
        )
        await self.record(
            EVENT_RUN,
            "broker.portfolio.loaded",
            f"Broker portfolio loaded: {len(baseline.positions)} position(s).",
            mode="LIVE",
        )

        failed = await analysis_service.refresh_history(
            self.db,
            user_id=self.user_id,
            connection=self.connection,
            universe=universe,
            ledger=baseline,
        )
        warnings.extend(f"No price history for {symbol}: {reason}." for symbol, reason in failed)
        await self.record(
            EVENT_RUN,
            "market_data.loaded",
            f"Daily market data refreshed for {len(universe.instruments)} "
            f"instrument(s) and {len(baseline.positions)} holding(s).",
            mode="LIVE",
        )
        return {"ledger_before": baseline, "warnings": warnings}

    async def create_paper_ledger(self, state: TradingState) -> dict[str, object]:
        """Start a NEW paper baseline from the current broker portfolio.

        A previous run's simulated portfolio is never reused as the starting
        point of a new analysis.
        """
        try:
            ledger = await ledger_service.create_ledger(
                self.db,
                user_id=self.user_id,
                connection=self.connection,
                country=self.country,
                universe=analysis_service.load_config(),
            )
        except ledger_service.LedgerError as exc:
            return await self.fail(state, str(exc))
        await self.record(
            EVENT_RUN,
            "ledger.created",
            f"Paper ledger created from the current broker portfolio "
            f"({len(ledger.positions)} position(s), cash {ledger.cash}).",
            mode="LIVE",
        )
        return {"ledger_before": ledger}

    async def build_analysis_context(self, state: TradingState) -> dict[str, object]:
        """Evidence plus the one context the model is shown."""
        ledger = state["ledger_before"]
        universe = analysis_service.load_config()
        wanted = analysis_service.history_symbols(universe, ledger)
        history = await analysis_service.load_history(
            self.db, user_id=self.user_id, connection=self.connection, symbols=wanted
        )
        evidence = {
            symbol: analysis_service.evidence_for(history.get(symbol, []))
            for symbol in sorted(wanted)
        }
        warnings = list(state.get("warnings") or [])
        warnings.extend(
            f"No price history is saved for {symbol}."
            for symbol in sorted(wanted)
            if not history.get(symbol)
        )
        warnings.extend(
            advice_service.offerable_watchlist(ledger=ledger, evidence=evidence, universe=universe)[
                1
            ]
        )
        return {
            "context": advice_service.analysis_context(
                country=self.country,
                ledger=ledger,
                evidence=evidence,
                universe=universe,
                warnings=warnings,
            ),
            "evidence": evidence,
            "prices": analysis_service.prices_from_evidence(evidence, ledger),
            "warnings": warnings,
        }

    async def analyse_portfolio(self, state: TradingState) -> dict[str, object]:
        """The ONE model call: the whole portfolio in, one target allocation out."""
        await self.record(
            EVENT_RUN,
            "ai.analysis.started",
            f"Asking the portfolio analyst for {self.country}.",
            mode="LIVE",
        )
        try:
            model, advice = await analysis_service.ask_advisor(self.model_key, state["context"])
        except analysis_service.AnalysisError as exc:
            await self.record(EVENT_RUN, "ai.analysis.failed", str(exc), mode="LIVE")
            return {"status": STATUS_FAILED, "message": str(exc)}
        await self.record(
            EVENT_RUN,
            "ai.analysis.completed",
            f"{model.label} answered with {len(advice.decisions)} decision(s).",
            mode="LIVE",
        )
        return {"model": model, "advice": advice}

    async def validate_advice(self, state: TradingState) -> dict[str, object]:
        """Refuse any target allocation that does not fit the account."""
        try:
            advice_view = analysis_service.validate_advice(
                state["advice"],
                model=state["model"],
                context=state["context"],
                universe=analysis_service.load_config(),
            )
        except analysis_service.AnalysisError as exc:
            await self.record(EVENT_RUN, "advice.rejected", str(exc), mode="LIVE")
            return {"status": STATUS_FAILED, "message": str(exc)}
        await self.record(
            EVENT_RUN,
            "advice.validated",
            f"Target allocation validated: cash "
            f"{advice_view.target_cash_weight:.0%}, "
            f"{len(advice_view.decisions)} decision(s).",
            mode="LIVE",
        )
        return {"advice_view": advice_view}

    async def apply_to_paper_ledger(self, state: TradingState) -> dict[str, object]:
        """Size the rebalance, apply it to the paper preview, save the plan.

        Simulation only: no broker call, no real order. The proposal holds the
        exact trades and the paper portfolio before and after.
        """
        try:
            plan = rebalance_service.plan_rebalance(
                advice=state["advice"],
                ledger=state["ledger_before"],
                universe=analysis_service.load_config(),
                prices=state.get("prices") or {},
            )
        except rebalance_service.RebalanceError as exc:
            await self.record(
                EVENT_RUN,
                "advice.rejected",
                f"The allocation could not be applied: {exc}",
                mode="LIVE",
            )
            return {"status": STATUS_FAILED, "message": str(exc)}

        proposal = analysis_service.build_rebalance_proposal(
            plan=plan,
            advice_view=state["advice_view"],
            evidence=state.get("evidence") or {},
            warnings=state.get("warnings") or [],
            model_key=getattr(state.get("model"), "key", None),
        )
        await ledger_service.save_ledger(
            self.db,
            user_id=self.user_id,
            connection=self.connection,
            ledger=plan.ledger_after,
        )
        await self.record(
            EVENT_RUN,
            "ledger.preview_applied",
            f"Paper preview applied: {len(plan.trades)} trade(s), "
            f"cash now {plan.ledger_after.cash}.",
            mode="LIVE",
        )
        record = await analysis_service.save_proposal(
            self.db,
            user_id=self.user_id,
            connection_id=self.connection.id,
            proposal=proposal,
        )
        await self.record(
            EVENT_PROPOSAL,
            "proposal.created",
            f"Proposal ID {record.id} saved with {len(proposal.orders)} order(s).",
            proposal_id=record.id,
            mode=proposal.approval_mode,
        )
        return {"proposal_id": record.id, "ledger_before": plan.ledger_before}

    # --- Human review -----------------------------------------------------

    async def request_approval(self, state: TradingState) -> dict[str, object]:
        """Open the human approval exactly once."""
        record, proposal = await self.proposal(state)
        if proposal.approval_mode != "HUMAN_IN_THE_LOOP":
            return await self.fail(
                state,
                f"Approval mode {proposal.approval_mode} is not enabled. "
                "Only human approval runs today.",
            )

        existing = await approvals_service.latest_approval(
            self.db, user_id=self.user_id, proposal_id=record.id
        )
        if existing is not None:
            return {"approval_id": existing.id}

        approval = await approvals_service.request_approval(
            self.db,
            user_id=self.user_id,
            connection_id=self.connection.id,
            proposal_id=record.id,
        )
        await self.record(
            EVENT_APPROVAL,
            "approval.requested",
            f"Approval requested for proposal {record.id}; it expires in "
            f"{approvals_service.APPROVAL_TTL_HOURS} hours.",
            proposal_id=record.id,
            mode=proposal.approval_mode,
        )
        return {"approval_id": approval.id}

    async def await_approval(self, state: TradingState) -> dict[str, object]:
        """Stop while the approval is pending; continue once it is approved."""
        approval = await approvals_service.get_approval(
            self.db, user_id=self.user_id, approval_id=state["approval_id"]
        )
        if approval is None:
            return await self.fail(state, "The approval for this proposal disappeared.")
        approval = await approvals_service.refresh_status(self.db, approval)

        if approval.status == APPROVAL_APPROVED:
            return {"approval_status": APPROVAL_APPROVED}
        if approval.status == APPROVAL_CONSUMED:
            return {
                "approval_status": APPROVAL_CONSUMED,
                "status": STATUS_COMPLETED,
                "message": "This proposal was already executed.",
            }
        if approval.status in {"rejected", "expired"}:
            rejected = approval.status == "rejected"
            await self.finalise(state, approval.status)
            await self.record(
                EVENT_APPROVAL,
                "approval.rejected",
                "The proposal was rejected; nothing was sent to the broker."
                if rejected
                else "The approval expired; nothing was sent to the broker.",
                proposal_id=state.get("proposal_id"),
            )
            return {
                "status": STATUS_REJECTED if rejected else STATUS_EXPIRED,
                "message": (
                    (approval.reason or "Rejected by the user.")
                    if rejected
                    else "The approval window expired before a decision."
                ),
            }
        return {
            "status": STATUS_AWAITING_APPROVAL,
            "approval_status": APPROVAL_PENDING,
            "paused": True,
        }

    # --- Execution --------------------------------------------------------

    async def execute_live(self, state: TradingState) -> dict[str, object]:
        """Submit one execution phase of the reviewed orders.

        A run submits either the sales or the purchases of the plan — each
        phase is its own explicit human confirmation. Buys are measured
        against the cash IOL actually reports, never against proceeds that a
        sale has not settled yet.
        """
        record, proposal = await self.proposal(state)
        phase = (state.get("phase") or "sells").lower()
        side = "BUY" if phase == "buys" else "SELL"

        cash_limit = None
        if side == "BUY":
            # Real buying power, refreshed from the broker before any buy.
            await broker_service.read_account_status(
                self.db, user_id=self.user_id, connection=self.connection
            )
            await broker_service.read_portfolio(
                self.db,
                user_id=self.user_id,
                connection=self.connection,
                country=proposal.country,
            )
            cash_limit = await broker_service.available_cash(
                self.db,
                user_id=self.user_id,
                connection=self.connection,
                currency=proposal.currency,
            )
            if cash_limit is None:
                message = "The broker reports no available cash, so no purchase can be funded yet."
                await self.record(
                    EVENT_RUN,
                    "run.blocked",
                    message,
                    proposal_id=record.id,
                    mode=proposal.execution_mode,
                )
                return {"status": STATUS_BLOCKED, "message": message}

        try:
            report = await execution_service.submit_proposal_orders(
                self.db,
                user_id=self.user_id,
                connection=self.connection,
                proposal_record=record,
                side=side,
                cash_limit=cash_limit,
            )
        except (execution_service.ExecutionError, ledger_service.LedgerError) as exc:
            await self.record(
                EVENT_RUN,
                "run.blocked",
                f"Execution was blocked: {exc}",
                proposal_id=record.id,
                mode=proposal.execution_mode,
            )
            return {"status": STATUS_BLOCKED, "message": str(exc)}

        if report.needs_reconciliation or report.rejected:
            message = (
                f"Execution did not complete: {report.rejected} rejected, {report.unknown} unknown."
            )
            await self.record(
                EVENT_RUN,
                "run.blocked",
                message,
                proposal_id=record.id,
                mode=proposal.execution_mode,
            )
            return {"status": STATUS_BLOCKED, "message": message}

        # The approval is consumed only when every order of the plan has been
        # submitted: a plan with purchases left keeps it for the next phase.
        buys_left = any(item.action == "BUY" for item in proposal.orders)
        done = side == "BUY" or not buys_left
        if done and not report.deferred:
            approval = await approvals_service.latest_approval(
                self.db, user_id=self.user_id, proposal_id=record.id
            )
            if approval is not None and approval.status == APPROVAL_APPROVED:
                await approvals_service.consume(self.db, approval)
            record.status = "approved"
            await self.db.commit()

        message = (
            f"{phase.capitalize()} phase finished: {report.accepted} accepted, "
            f"{report.filled} filled, {report.skipped} already handled"
            + (f", {len(report.deferred)} deferred without funding" if report.deferred else "")
            + "."
        )
        await self.record(
            EVENT_RUN,
            "run.completed",
            message,
            proposal_id=record.id,
            mode=proposal.execution_mode,
            payload={
                "phase": phase,
                "accepted": report.accepted,
                "filled": report.filled,
                "rejected": report.rejected,
                "unknown": report.unknown,
                "skipped": report.skipped,
                "deferred": report.deferred,
            },
        )
        return {"status": STATUS_COMPLETED, "message": message}


# --- Routing ---------------------------------------------------------------


def route_start(state: TradingState) -> str:
    return "resume" if state.get("proposal_id") else "fresh"


def route_ok(state: TradingState) -> str:
    return "failed" if state.get("status") == STATUS_FAILED else "ok"


def route_after_approval(state: TradingState) -> str:
    return "execute_live" if state.get("approval_status") == APPROVAL_APPROVED else "end"


def build_graph(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    country: str,
    model_key: str | None = None,
):
    """Compile the trading graph for one connection and country."""
    run = Run(
        db=db,
        user_id=user_id,
        connection=connection,
        country=country,
        model_key=model_key,
    )
    graph = StateGraph(TradingState)
    graph.add_node("load_broker_state", run.load_broker_state)
    graph.add_node("create_paper_ledger", run.create_paper_ledger)
    graph.add_node("build_analysis_context", run.build_analysis_context)
    graph.add_node("analyse_portfolio", run.analyse_portfolio)
    graph.add_node("validate_advice", run.validate_advice)
    graph.add_node("apply_to_paper_ledger", run.apply_to_paper_ledger)
    graph.add_node("request_approval", run.request_approval)
    graph.add_node("await_approval", run.await_approval)
    graph.add_node("execute_live", run.execute_live)

    graph.add_conditional_edges(
        START,
        route_start,
        {"fresh": "load_broker_state", "resume": "await_approval"},
    )
    chain = [
        "load_broker_state",
        "create_paper_ledger",
        "build_analysis_context",
        "analyse_portfolio",
        "validate_advice",
        "apply_to_paper_ledger",
    ]
    for node, following in zip(chain, chain[1:], strict=False):
        graph.add_conditional_edges(node, route_ok, {"failed": END, "ok": following})
    graph.add_conditional_edges(
        "apply_to_paper_ledger", route_ok, {"failed": END, "ok": "request_approval"}
    )
    graph.add_edge("request_approval", "await_approval")
    graph.add_conditional_edges(
        "await_approval",
        route_after_approval,
        {"execute_live": "execute_live", "end": END},
    )
    graph.add_edge("execute_live", END)
    return graph.compile()


def _initial_state(
    *, proposal_id: int | None, approval_id: int | None, phase: str | None = None
) -> dict[str, object]:
    return {
        "proposal_id": proposal_id,
        "approval_id": approval_id,
        "status": STATUS_RUNNING,
        "paused": False,
        "approval_status": None,
        "message": None,
        "phase": phase,
    }


def _run_config(
    *,
    connection: BrokerConnection,
    country: str,
    model_key: str | None,
    proposal_id: int | None = None,
) -> dict[str, object]:
    """The LangSmith metadata one graph run may show. Safe values only."""
    provider = model_name = None
    if model_key:
        try:
            entry = advice_service.resolve_model(model_key)
            provider, model_name = entry.provider, entry.model
        except Exception:  # noqa: BLE001 - metadata must never break a run
            provider = model_name = model_key
    return {
        "metadata": run_metadata(
            connection_id=connection.id,
            country=country,
            model_provider=provider,
            model_name=model_name,
            proposal_id=proposal_id,
            execution_mode="LIVE",
        )
    }


async def start_run(
    db: AsyncSession,
    *,
    user_id: int,
    connection: BrokerConnection,
    country: str,
    model_key: str | None = None,
    phase: str | None = None,
) -> dict[str, object]:
    """Run the full analysis and pause it for human approval."""
    graph = build_graph(
        db,
        user_id=user_id,
        connection=connection,
        country=country,
        model_key=model_key,
    )
    config = _run_config(connection=connection, country=country, model_key=model_key)
    return await graph.ainvoke(
        _initial_state(proposal_id=None, approval_id=None, phase=phase), config=config
    )


async def resume_run(
    db: AsyncSession,
    *,
    user_id: int,
    proposal_id: int,
    phase: str | None = None,
) -> dict[str, object]:
    """Continue a paused run after its approval was saved.

    The analysis nodes are not re-entered: no second model call, no new
    proposal. The graph re-derives its position from the saved rows. ``phase``
    selects which execution pass to submit (``"sells"`` or ``"buys"``).
    """
    record = await analysis_service.get_proposal(db, user_id=user_id, proposal_id=proposal_id)
    if record is None:
        raise WorkflowError("Proposal not found.")
    connection = await connections_service.get_connection(
        db, user_id=user_id, connection_id=record.connection_id
    )
    approval = await approvals_service.latest_approval(db, user_id=user_id, proposal_id=proposal_id)
    graph = build_graph(db, user_id=user_id, connection=connection, country=record.country)
    config = _run_config(
        connection=connection,
        country=record.country,
        model_key=None,
        proposal_id=proposal_id,
    )
    return await graph.ainvoke(
        _initial_state(
            proposal_id=proposal_id,
            approval_id=approval.id if approval else None,
            phase=phase,
        ),
        config=config,
    )
