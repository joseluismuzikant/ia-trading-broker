"""Analysis pages: the paper ledger and the proposal review page.

The page always renders saved data: the newest paper ledger and the newest
proposal. Running an analysis refreshes price history through the broker and
stores a new proposal. Nothing here places an order.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.broker import _resolve_country, _select_connection
from app.db import get_db
from app.deps import AuthContext, require_user, validate_csrf_pair
from app.domain.trading import Proposal
from app.infrastructure.iol import call_budget
from app.services import analysis as analysis_service
from app.services import broker as broker_service
from app.services import connections as connections_service
from app.services import ledger as ledger_service
from app.templating import templates

router = APIRouter(tags=["analysis"])

#: Market used when a symbol's market is unknown. BCBA is the local exchange.
DEFAULT_MARKET = "BCBA"


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
    proposals: list = []

    if connection is not None:
        selected = _resolve_country(country, connection)
        ledger = await ledger_service.load_ledger(
            db, user_id=auth.user.id, connection=connection, country=selected
        )

        if proposal_id is not None:
            record = await analysis_service.get_proposal(
                db, user_id=auth.user.id, proposal_id=proposal_id
            )
            if record is not None:
                proposal = Proposal.model_validate(record.payload)
                proposal = proposal.model_copy(update={"status": record.status})
        if proposal is None:
            records = await analysis_service.list_proposals(
                db,
                user_id=auth.user.id,
                connection_id=connection.id,
                country=selected,
            )
            proposals = records[:5]
            if records:
                proposal = Proposal.model_validate(records[0].payload)
                proposal = proposal.model_copy(update={"status": records[0].status})

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
            "proposal": proposal,
            "proposals": proposals,
            "ledger_status": request.query_params.get("ledger"),
            "analysis_status": request.query_params.get("analysis"),
            "message": request.query_params.get("message"),
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

    # Refresh price history for every symbol the analysis will judge. A failed
    # read is not fatal: the symbol is judged on whatever history is saved.
    symbols = {position.symbol for position in ledger.positions}
    symbols |= analysis_service.DEFAULT_ALLOWLIST
    for symbol in sorted(symbols):
        market = _market_for(symbol, ledger)
        try:
            await broker_service.read_price_history(
                db,
                user_id=auth.user.id,
                connection=connection,
                market=market,
                symbol=symbol,
            )
        except Exception:  # noqa: BLE001 - a bad market must not stop the run
            continue

    try:
        record = await analysis_service.run_analysis(
            db, user_id=auth.user.id, connection=connection, country=selected
        )
    except analysis_service.AnalysisError as exc:
        await db.rollback()
        return RedirectResponse(
            f"{base}&analysis=error&message={str(exc)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    return RedirectResponse(
        f"{base}&analysis=ok&proposal_id={record.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _market_for(symbol: str, ledger) -> str:
    """The market to query for a symbol: the ledger's, or the local default."""
    position = ledger.position_for(symbol)
    if position is not None and position.market:
        return position.market
    return DEFAULT_MARKET
