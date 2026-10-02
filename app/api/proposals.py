"""Proposal review page and the human approval actions.

The review page is where a paused run is decided. Approving calls the workflow
again, which re-checks the saved data and submits the plan to the paper
executor. Rejecting closes the run with no order. Both actions are CSRF-protected
and both are single-use: the approval service refuses a second decision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import AuthContext, require_user, validate_csrf_pair
from app.domain.trading import Proposal
from app.infrastructure.iol import call_budget
from app.services import analysis as analysis_service
from app.services import approvals as approvals_service
from app.services import connections as connections_service
from app.services import events as events_service
from app.services import ledger as ledger_service
from app.services import orders as orders_service
from app.templating import templates
from app.workflows import trading_flow

router = APIRouter(tags=["proposals"])


async def _load_record(db: AsyncSession, *, user_id: int, proposal_id: int):
    """Return the user's proposal or raise 404 (mismatches look like 404)."""
    record = await analysis_service.get_proposal(
        db, user_id=user_id, proposal_id=proposal_id
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found."
        )
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

    approval = await approvals_service.latest_approval(
        db, user_id=user_id, proposal_id=proposal_id
    )
    if approval is not None:
        # A pending approval past its window is shown as expired.
        approval = await approvals_service.refresh_status(db, approval)

    proposal = Proposal.model_validate(record.payload).model_copy(
        update={"status": record.status}
    )
    orders = await orders_service.list_orders_for_proposal(
        db, user_id=user_id, proposal_id=proposal_id
    )
    events = await events_service.list_events_for_proposal(
        db, user_id=user_id, proposal_id=proposal_id
    )
    ledger = await ledger_service.load_ledger(
        db, user_id=user_id, connection=connection, country=record.country
    )
    return {
        "connection": connection,
        "proposal": proposal,
        "proposal_record": record,
        "universe": analysis_service.load_config(),
        "approval": approval,
        "orders": orders,
        "events": events,
        "ledger": ledger,
    }


@router.get("/proposals/{proposal_id}", response_class=HTMLResponse)
async def proposal_review(
    request: Request,
    proposal_id: int,
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Show one proposal with its approval, orders, and timeline."""
    context = await _review_context(
        db, user_id=auth.user.id, proposal_id=proposal_id
    )
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
    context = await _review_context(
        db, user_id=auth.user.id, proposal_id=proposal_id
    )
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
    """Approve once and continue the paused run."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)
    await _load_record(db, user_id=auth.user.id, proposal_id=proposal_id)

    try:
        await approvals_service.approve(
            db, user_id=auth.user.id, proposal_id=proposal_id
        )
    except approvals_service.ApprovalError as exc:
        return _redirect(proposal_id, "error", message=str(exc))

    try:
        state = await trading_flow.resume_run(
            db, user_id=auth.user.id, proposal_id=proposal_id
        )
    except trading_flow.WorkflowError as exc:
        return _redirect(proposal_id, "error", message=str(exc))

    return _redirect(
        proposal_id,
        "ok",
        run=state.get("status") or trading_flow.STATUS_COMPLETED,
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
        await trading_flow.resume_run(
            db, user_id=auth.user.id, proposal_id=proposal_id
        )
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
