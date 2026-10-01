"""Analysis pages: the paper ledger and the proposal review page.

The page always renders saved data: the newest paper ledger and the newest
proposal. Running an analysis refreshes price history through the broker and
stores a new proposal. Nothing here places an order.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.broker import _resolve_country, _select_connection
from app.db import get_db
from app.deps import AuthContext, require_user, validate_csrf_pair
from app.domain.trading import Proposal
from app.infrastructure.iol import call_budget
from app.services import analysis as analysis_service
from app.services import approvals as approvals_service
from app.services import connections as connections_service
from app.services import ledger as ledger_service
from app.services import orders as orders_service
from app.templating import templates
from app.workflows import trading_flow

router = APIRouter(tags=["analysis"])


async def _render(
    request: Request,
    auth: AuthContext,
    db: AsyncSession,
    *,
    connection_id: int | None,
    country: str | None,
    proposal_id: int | None,
) -> HTMLResponse:
    """Load the ledger and the proposal to show, then render the page."""
    connection = await _select_connection(
        db, user_id=auth.user.id, connection_id=connection_id
    )
    connections = await connections_service.list_connections(db, user_id=auth.user.id)

    selected = country
    ledger = None
    proposal = None
    proposal_record = None
    proposals: list = []
    approval = None
    orders: list = []

    if connection is not None:
        selected = _resolve_country(country, connection)
        ledger = await ledger_service.load_ledger(
            db, user_id=auth.user.id, connection=connection, country=selected
        )

        if proposal_id is not None:
            proposal_record = await analysis_service.get_proposal(
                db, user_id=auth.user.id, proposal_id=proposal_id
            )
        if proposal_record is None:
            records = await analysis_service.list_proposals(
                db,
                user_id=auth.user.id,
                connection_id=connection.id,
                country=selected,
            )
            proposals = records[:5]
            proposal_record = records[0] if records else None
        if proposal_record is not None:
            proposal = Proposal.model_validate(proposal_record.payload)
            proposal = proposal.model_copy(update={"status": proposal_record.status})
            approval = await approvals_service.latest_approval(
                db, user_id=auth.user.id, proposal_id=proposal_record.id
            )
            orders = await orders_service.list_orders_for_proposal(
                db, user_id=auth.user.id, proposal_id=proposal_record.id
            )

    return templates.TemplateResponse(
        request,
        "analysis.html",
        {
            "user": auth.user,
            "csrf_token": auth.session.csrf_token,
            "connection": connection,
            "connections": connections,
            "country": selected,
            "countries": connections_service.SUPPORTED_COUNTRIES,
            "ledger": ledger,
            "universe": analysis_service.load_config(),
            "proposal": proposal,
            "proposal_record": proposal_record,
            "proposals": proposals,
            "approval": approval,
            "orders": orders,
            "ledger_status": request.query_params.get("ledger"),
            "analysis_status": request.query_params.get("analysis"),
            "message": request.query_params.get("message"),
            "history_message": request.query_params.get("history"),
            "iol_usage": call_budget.usage,
        },
    )


@router.get("/analysis", response_class=HTMLResponse)
async def analysis_page(
    request: Request,
    auth: AuthContext = Depends(require_user),
    connection_id: int | None = None,
    country: str | None = None,
    proposal_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Show the paper ledger and the newest (or selected) proposal."""
    return await _render(
        request,
        auth,
        db,
        connection_id=connection_id,
        country=country,
        proposal_id=proposal_id,
    )


@router.post("/analysis/ledger")
async def create_ledger(
    auth: AuthContext = Depends(require_user),
    connection_id: int | None = Form(default=None),
    country: str = Form(default="argentina"),
    csrf_token: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Create the paper ledger from the newest portfolio snapshot."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)

    connection = await _select_connection(
        db, user_id=auth.user.id, connection_id=connection_id
    )
    if connection is None:
        return RedirectResponse("/analysis", status_code=status.HTTP_303_SEE_OTHER)

    selected = _resolve_country(country, connection)
    base = f"/analysis?connection_id={connection.id}&country={selected}"
    try:
        await ledger_service.create_ledger(
            db, user_id=auth.user.id, connection=connection, country=selected
        )
    except ledger_service.LedgerError as exc:
        await db.rollback()
        return RedirectResponse(
            f"{base}&ledger=error&message={str(exc)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(f"{base}&ledger=ok", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/analysis/run")
async def run_analysis(
    auth: AuthContext = Depends(require_user),
    connection_id: int | None = Form(default=None),
    country: str = Form(default="argentina"),
    csrf_token: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Refresh price history, run the strategy and policy, save a proposal."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)

    connection = await _select_connection(
        db, user_id=auth.user.id, connection_id=connection_id
    )
    if connection is None:
        return RedirectResponse("/analysis", status_code=status.HTTP_303_SEE_OTHER)

    selected = _resolve_country(country, connection)
    base = f"/analysis?connection_id={connection.id}&country={selected}"

    ledger = await ledger_service.load_ledger(
        db, user_id=auth.user.id, connection=connection, country=selected
    )
    if ledger is None:
        return RedirectResponse(
            f"{base}&analysis=error&message=Create+a+paper+ledger+first.",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # The funnel needs a price history for every configured instrument and for
    # everything the ledger holds. A failed read is not fatal: the symbol is
    # judged on whatever history is saved, and the failure is reported rather
    # than disguised as "not enough history".
    universe = analysis_service.load_config()
    failed = await analysis_service.refresh_history(
        db,
        user_id=auth.user.id,
        connection=connection,
        universe=universe,
        ledger=ledger,
    )

    history_message = _run_note(failed, universe=universe)
    # Build the proposal through the workflow, which then pauses for the human
    # approval when the plan holds at least one order.
    state = await trading_flow.start_run(
        db, user_id=auth.user.id, connection=connection, country=selected
    )

    proposal_id = state.get("proposal_id")
    if proposal_id:
        suffix = f"&history={quote(history_message)}" if history_message else ""
        return RedirectResponse(
            f"{base}&analysis=ok&proposal_id={proposal_id}{suffix}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    message = state.get("message") or "The analysis did not run."
    return RedirectResponse(
        f"{base}&analysis=error&message={message}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _run_note(failed: list[tuple[str, str]], *, universe) -> str:
    """One message naming everything the run could not read, or "" when it could."""
    parts: list[str] = []
    if not universe.instruments:
        parts.append(
            "The configured universe holds no instruments, so no new position "
            "was proposed."
        )
    if failed:
        described = ", ".join(f"{symbol} ({reason})" for symbol, reason in failed)
        parts.append(
            "Could not refresh the price history for "
            + described
            + ". Those symbols are judged on saved data and may show as not "
            "enough history."
        )
    return " ".join(parts)
