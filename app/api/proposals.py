"""Proposal review page and the human approval actions.

The review page is where a paused run is decided. Approving calls the workflow
again, which re-checks the saved data and submits the plan to the paper
executor. Rejecting closes the run with no order. Both actions are CSRF-protected
and both are single-use: the approval service refuses a second decision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import AuthContext, require_user, validate_csrf_pair
from app.domain.money import money
from app.domain.trading import Proposal
from app.infrastructure.iol import call_budget
from app.services import analysis as analysis_service
from app.services import approvals as approvals_service
from app.services import broker as broker_service
from app.services import connections as connections_service
from app.services import events as events_service
from app.services import execution as execution_service
from app.services import ledger as ledger_service
from app.services import orders as orders_service
from app.services import progress as progress_service
from app.templating import templates
from app.workflows import trading_flow

router = APIRouter(tags=["proposals"])


async def _load_record(db: AsyncSession, *, user_id: int, proposal_id: int):
    """Return the user's proposal or raise 404 (mismatches look like 404)."""
    record = await analysis_service.get_proposal(db, user_id=user_id, proposal_id=proposal_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found.")
    return record


async def _review_context(
    db: AsyncSession,
    *,
    user_id: int,
    proposal_id: int,
) -> dict:
    """Everything a proposal page shows: the plan and how it was decided."""
    record = await _load_record(db, user_id=user_id, proposal_id=proposal_id)
    connection = await connections_service.get_connection(
        db, user_id=user_id, connection_id=record.connection_id
    )

    approval = await approvals_service.latest_approval(db, user_id=user_id, proposal_id=proposal_id)
    if approval is not None:
        # A pending approval past its window is shown as expired.
        approval = await approvals_service.refresh_status(db, approval)

    proposal = Proposal.model_validate(record.payload).model_copy(update={"status": record.status})
    orders = await orders_service.list_orders_for_proposal(
        db, user_id=user_id, proposal_id=proposal_id
    )
    events = await events_service.list_events_for_proposal(
        db, user_id=user_id, proposal_id=proposal_id
    )
    ledger = await ledger_service.load_ledger(
        db, user_id=user_id, connection=connection, country=record.country
    )
    orders_list = proposal.orders
    sells = [item for item in orders_list if item.action == "SELL"]
    buys = [item for item in orders_list if item.action == "BUY"]
    sell_proceeds = money(sum((item.notional.amount if item.notional else 0) for item in sells))
    buy_cost = money(sum((item.notional.amount if item.notional else 0) for item in buys))
    cash_after = (
        proposal.ledger_after.cash
        if proposal.ledger_after is not None
        else money(proposal.cash.amount + sell_proceeds.amount - buy_cost.amount)
    )
    deletable = all(order.status not in analysis_service.UNSETTLED_ORDER_STATES for order in orders)
    sell_rows = [order for order in orders if order.side == "SELL"]
    buy_rows = [order for order in orders if order.side == "BUY"]
    sells_submitted = bool(sells) and len(sell_rows) >= len(sells)
    buys_submitted = bool(buys) and len(buy_rows) >= len(buys)
    sells_remaining = bool(sells) and not sells_submitted
    buys_remaining = bool(buys) and not buys_submitted
    # The newest order per trade: what the per-trade Execute button must show.
    latest_by_trade: dict[str, object] = {}
    for order in orders:
        latest_by_trade[f"{order.symbol}:{order.side}"] = order
    unsettled = any(order.status in analysis_service.UNSETTLED_ORDER_STATES for order in orders)
    decided = approval is not None and approval.status in ("approved", "consumed")
    resumable = decided and (sells_remaining or buys_remaining or unsettled)
    execution_complete = decided and not resumable

    def _count(statuses: set[str]) -> int:
        return sum(1 for order in orders if order.status in statuses)

    cash_available = await broker_service.available_cash(
        db, user_id=user_id, connection=connection, currency=proposal.currency
    )
    steps = progress_service.proposal_steps(
        approval_status=approval.status if approval else None,
        has_sells=bool(sells),
        has_buys=bool(buys),
        sells_submitted=sells_submitted,
        buys_submitted=buys_submitted,
        any_submitted=bool(orders),
        pending_orders=_count({"accepted", "submitted", "prepared", "partially_filled"}),
    )
    return {
        "steps": steps,
        "connection": connection,
        "proposal": proposal,
        "proposal_record": record,
        "universe": analysis_service.load_config(),
        "approval": approval,
        "orders": orders,
        "events": events,
        "ledger": ledger,
        "sells": sells,
        "buys": buys,
        "sell_rows": sell_rows,
        "buy_rows": buy_rows,
        "sells_submitted": sells_submitted,
        "buys_submitted": buys_submitted,
        "sells_remaining": sells_remaining,
        "buys_remaining": buys_remaining,
        "latest_by_trade": latest_by_trade,
        "resumable": resumable,
        "execution_complete": execution_complete,
        "any_submitted": bool(orders),
        "cash_available": cash_available,
        "filled_count": _count({"filled"}),
        "partial_count": _count({"partially_filled"}),
        "pending_count": _count({"accepted", "submitted", "prepared"}),
        "failed_count": _count({"rejected", "unknown"}),
        "sell_proceeds": sell_proceeds,
        "buy_cost": buy_cost,
        "cash_after": cash_after,
        "deletable": deletable,
        "model_label": proposal.advice.model_label if proposal.advice else None,
    }


@router.get("/proposals", response_class=HTMLResponse)
async def proposals_list(
    request: Request,
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """List the signed-in user's proposals, newest first.

    Owner-scoped: another account's plans are never loaded here, whatever
    connection they belong to.
    """
    records = await analysis_service.list_all_proposals(db, user_id=auth.user.id)
    connections = await connections_service.list_connections(db, user_id=auth.user.id)
    labels = {connection.id: connection.label for connection in connections}
    all_orders = await orders_service.list_orders(db, user_id=auth.user.id, limit=1000)
    by_proposal: dict[int, list] = {}
    for order in all_orders:
        if order.proposal_id is not None:
            by_proposal.setdefault(order.proposal_id, []).append(order)

    rows = []
    for record in records:
        proposal_orders = by_proposal.get(record.id, [])
        deletable = all(
            order.status not in analysis_service.UNSETTLED_ORDER_STATES for order in proposal_orders
        )
        try:
            proposal = Proposal.model_validate(record.payload)
        except Exception:  # noqa: BLE001 - an unreadable payload must not hide the rest
            rows.append(
                {
                    "record": record,
                    "proposal": None,
                    "model_label": "—",
                    "trades": 0,
                    "portfolio_value": None,
                    "currency": None,
                    "deletable": deletable,
                }
            )
            continue
        rows.append(
            {
                "record": record,
                "proposal": proposal,
                "model_label": (proposal.advice.model_label if proposal.advice else "—"),
                "trades": len(proposal.orders),
                "portfolio_value": proposal.portfolio_value,
                "currency": proposal.currency,
                "deletable": deletable,
            }
        )

    return templates.TemplateResponse(
        request,
        "proposals.html",
        {
            "user": auth.user,
            "csrf_token": auth.session.csrf_token,
            "rows": rows,
            "connection_labels": labels,
        },
    )


@router.post("/proposals/{proposal_id}/delete")
async def delete_proposal(
    proposal_id: int,
    csrf_token: str = Form(default=""),
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Delete one owned proposal, when nothing is pending at the broker."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)
    await _load_record(db, user_id=auth.user.id, proposal_id=proposal_id)

    try:
        await analysis_service.delete_proposal(db, user_id=auth.user.id, proposal_id=proposal_id)
    except analysis_service.ProposalDeleteError as exc:
        return RedirectResponse(
            f"/proposals?error={quote(str(exc))}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse("/proposals?deleted=1", status_code=status.HTTP_303_SEE_OTHER)


async def _require_decision(approval) -> str | None:
    """None when a human decision exists and is still fresh, else the reason it does not."""
    return approvals_service.execution_problem(approval)


@router.post("/proposals/{proposal_id}/execute-sells")
async def execute_sells(
    proposal_id: int,
    csrf_token: str = Form(default=""),
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Explicit confirmation of PHASE 1: submit the reviewed SELL orders."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)
    await _load_record(db, user_id=auth.user.id, proposal_id=proposal_id)
    approval = await approvals_service.latest_approval(
        db, user_id=auth.user.id, proposal_id=proposal_id
    )
    problem = await _require_decision(approval)
    if problem:
        return _redirect(proposal_id, "error", message=problem)

    try:
        state = await trading_flow.resume_run(
            db, user_id=auth.user.id, proposal_id=proposal_id, phase="sells"
        )
    except trading_flow.WorkflowError as exc:
        return _redirect(proposal_id, "error", message=str(exc))
    return _redirect(proposal_id, "ok", run=state.get("status") or trading_flow.STATUS_COMPLETED)


@router.post("/proposals/{proposal_id}/execute-buys")
async def execute_buys(
    proposal_id: int,
    csrf_token: str = Form(default=""),
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Explicit confirmation of PHASE 2: submit the affordable BUY orders.

    The cash IOL currently reports decides what is executable; nothing is
    bought against a sale that has not settled.
    """
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)
    await _load_record(db, user_id=auth.user.id, proposal_id=proposal_id)
    approval = await approvals_service.latest_approval(
        db, user_id=auth.user.id, proposal_id=proposal_id
    )
    problem = await _require_decision(approval)
    if problem:
        return _redirect(proposal_id, "error", message=problem)

    try:
        state = await trading_flow.resume_run(
            db, user_id=auth.user.id, proposal_id=proposal_id, phase="buys"
        )
    except trading_flow.WorkflowError as exc:
        return _redirect(proposal_id, "error", message=str(exc))
    return _redirect(proposal_id, "ok", run=state.get("status") or trading_flow.STATUS_COMPLETED)


@router.post("/proposals/{proposal_id}/reconcile")
async def reconcile_orders(
    proposal_id: int,
    csrf_token: str = Form(default=""),
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Refresh the status of every submitted order from IOL. Reads only."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)
    record = await _load_record(db, user_id=auth.user.id, proposal_id=proposal_id)
    connection = await connections_service.get_connection(
        db, user_id=auth.user.id, connection_id=record.connection_id
    )
    try:
        counts = await execution_service.reconcile_proposal_orders(
            db,
            user_id=auth.user.id,
            connection=connection,
            proposal_record=record,
        )
    except Exception as exc:  # noqa: BLE001 - one clear message on the page
        return _redirect(proposal_id, "error", message=f"Reconciliation failed: {exc}")
    summary = (
        f"Reconciled with IOL: {counts['filled']} filled, "
        f"{counts['partial']} partial, {counts['pending']} pending, "
        f"{counts['cancelled']} cancelled, {counts['rejected']} rejected."
    )
    if counts["unresolved"]:
        summary += (
            f" {counts['unresolved']} order(s) never got an operation number, so IOL "
            "could not be asked about them. Check them in your IOL account; they stay "
            "blocked from being sent again."
        )
    return _redirect(proposal_id, "ok", run=summary)


@router.post("/proposals/{proposal_id}/execute")
async def execute_trade(
    proposal_id: int,
    symbol: str = Form(default=""),
    side: str = Form(default=""),
    quantity: str = Form(default=""),
    csrf_token: str = Form(default=""),
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Execute one trade, at the amount the user chose, whenever it suits.

    The quantity comes from the form (prefilled with the plan's suggestion and
    editable); the price and instrument metadata come from the reviewed plan.
    The same trade may be sent again only after a proven failure — a rejected
    or cancelled order — never while one is still at the broker or unknown.
    """
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)
    record = await _load_record(db, user_id=auth.user.id, proposal_id=proposal_id)
    approval = await approvals_service.latest_approval(
        db, user_id=auth.user.id, proposal_id=proposal_id
    )
    problem = await _require_decision(approval)
    if problem:
        return _redirect(proposal_id, "error", message=problem)
    connection = await connections_service.get_connection(
        db, user_id=auth.user.id, connection_id=record.connection_id
    )
    if connection is None:
        return _redirect(proposal_id, "error", message="The connection is gone.")

    try:
        units = Decimal(quantity.strip())
    except Exception:  # noqa: BLE001 - any bad input is one clear message
        return _redirect(proposal_id, "error", message="Quantity must be a whole number of units.")

    try:
        report = await execution_service.execute_single_order(
            db,
            user_id=auth.user.id,
            connection=connection,
            proposal_record=record,
            symbol=symbol.strip().upper(),
            side=side.strip().upper(),
            units=units,
        )
    except Exception as exc:  # noqa: BLE001 - one clear message on the page
        return _redirect(proposal_id, "error", message=str(exc))

    if report.skipped:
        message = f"{side} {symbol}: already sent; nothing was sent twice."
    else:
        message = (
            f"{side} {units} {symbol}: {report.accepted} accepted, "
            f"{report.filled} filled, {report.rejected} rejected, "
            f"{report.unknown} unknown."
        )
    return _redirect(proposal_id, "ok", run=message)


@router.post("/proposals/{proposal_id}/resume")
async def resume_execution(
    proposal_id: int,
    csrf_token: str = Form(default=""),
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Continue execution until every order is submitted.

    Re-runs the pending phase — sales first, then purchases — and first
    refreshes every submitted order from IOL, so a previously-unknown order is
    resolved before the next one is sent. Idempotent by design: an order
    already at the broker is never sent again, so this is safe to press
    repeatedly until the plan is done.
    """
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)
    record = await _load_record(db, user_id=auth.user.id, proposal_id=proposal_id)
    approval = await approvals_service.latest_approval(
        db, user_id=auth.user.id, proposal_id=proposal_id
    )
    problem = await _require_decision(approval)
    if problem:
        return _redirect(proposal_id, "error", message=problem)
    connection = await connections_service.get_connection(
        db, user_id=auth.user.id, connection_id=record.connection_id
    )
    if connection is None:
        return _redirect(proposal_id, "error", message="The connection is gone.")

    # Refresh first: an unknown order must be resolved before anything else.
    await execution_service.reconcile_proposal_orders(
        db, user_id=auth.user.id, connection=connection, proposal_record=record
    )

    proposal = Proposal.model_validate(record.payload)
    orders = await orders_service.list_orders_for_proposal(
        db, user_id=auth.user.id, proposal_id=proposal_id
    )
    sell_rows = [order for order in orders if order.side == "SELL"]
    buy_rows = [order for order in orders if order.side == "BUY"]
    sells = [item for item in proposal.orders if item.action == "SELL"]
    buys = [item for item in proposal.orders if item.action == "BUY"]

    if sells and len(sell_rows) < len(sells):
        phase = "sells"
    elif buys and len(buy_rows) < len(buys):
        phase = "buys"
    else:
        return _redirect(
            proposal_id,
            "ok",
            run="Execution is fully submitted. Refresh Order Status to follow the fills.",
        )

    state = await trading_flow.resume_run(
        db, user_id=auth.user.id, proposal_id=proposal_id, phase=phase
    )
    return _redirect(proposal_id, "ok", run=state.get("status") or trading_flow.STATUS_COMPLETED)


@router.get("/proposals/{proposal_id}", response_class=HTMLResponse)
async def proposal_review(
    request: Request,
    proposal_id: int,
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Show one proposal with its approval, orders, and timeline."""
    context = await _review_context(db, user_id=auth.user.id, proposal_id=proposal_id)
    return templates.TemplateResponse(
        request,
        "proposal.html",
        {
            "user": auth.user,
            "csrf_token": auth.session.csrf_token,
            **context,
            "approval_status": request.query_params.get("approval"),
            "run_status": request.query_params.get("run"),
            "message": request.query_params.get("message"),
            "iol_usage": call_budget.usage,
        },
    )


@router.get("/proposals/{proposal_id}/export", response_class=HTMLResponse)
async def proposal_export(
    request: Request,
    proposal_id: int,
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Render one proposal as a printable document.

    The page is self-contained and print-optimised so the browser can save it
    as a PDF ("Export PDF", then "Save as PDF" in the print dialog). It shows
    the same data as the review page and can only be read by the proposal's
    owner.
    """
    context = await _review_context(db, user_id=auth.user.id, proposal_id=proposal_id)
    return templates.TemplateResponse(
        request,
        "proposal_export.html",
        {
            "user": auth.user,
            **context,
            "generated_at": datetime.now(UTC),
        },
    )


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: int,
    csrf_token: str = Form(default=""),
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Approve the plan once. Nothing is sent by this alone.

    Approval records the human decision; each trade is then executed
    separately, with its own quantity, whenever the portfolio allows it.
    """
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)
    await _load_record(db, user_id=auth.user.id, proposal_id=proposal_id)

    try:
        await approvals_service.approve(db, user_id=auth.user.id, proposal_id=proposal_id)
    except approvals_service.ApprovalError as exc:
        return _redirect(proposal_id, "error", message=str(exc))

    return _redirect(
        proposal_id,
        "ok",
        run="Plan approved. Execute the trades below when you are ready.",
    )


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: int,
    reason: str = Form(default=""),
    csrf_token: str = Form(default=""),
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Reject once and close the run without an order."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)
    await _load_record(db, user_id=auth.user.id, proposal_id=proposal_id)

    try:
        await approvals_service.reject(
            db,
            user_id=auth.user.id,
            proposal_id=proposal_id,
            reason=reason or None,
        )
    except approvals_service.ApprovalError as exc:
        return _redirect(proposal_id, "error", message=str(exc))

    try:
        await trading_flow.resume_run(db, user_id=auth.user.id, proposal_id=proposal_id)
    except trading_flow.WorkflowError:
        # The rejection is already saved; the timeline just missed a step.
        pass

    return _redirect(proposal_id, "rejected")


def _redirect(
    proposal_id: int,
    approval: str,
    *,
    run: str | None = None,
    message: str | None = None,
) -> RedirectResponse:
    """Return to the review page with a short status code."""
    location = f"/proposals/{proposal_id}?approval={approval}"
    if run:
        location += f"&run={quote(run)}"
    if message:
        location += f"&message={quote(message)}"
    return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)
