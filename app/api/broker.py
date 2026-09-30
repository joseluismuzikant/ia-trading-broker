"""Profile, account-status, and country-portfolio pages.

Both kinds of page follow the same rule: render the newest saved snapshot
immediately, and refresh only when the user asks. A failed refresh keeps the
previous data on screen and marks it stale.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import AuthContext, require_user, validate_csrf_pair
from app.domain.portfolio import Portfolio
from app.infrastructure.iol import call_budget
from app.models import (
    SNAPSHOT_ACCOUNT_STATUS,
    SNAPSHOT_PROFILE,
    BrokerConnection,
    portfolio_snapshot_kind,
)
from app.services import broker as broker_service
from app.services import connections as connections_service
from app.templating import templates

router = APIRouter(tags=["broker"])


async def _select_connection(
    db: AsyncSession,
    *,
    user_id: int,
    connection_id: int | None,
) -> BrokerConnection | None:
    """Pick the connection a page should show.

    An explicit ``connection_id`` from the query string wins. Otherwise the
    first active connection is used, so a single-connection user never has to
    choose anything.
    """
    if connection_id is not None:
        try:
            return await connections_service.get_connection(
                db, user_id=user_id, connection_id=connection_id
            )
        except connections_service.ConnectionNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found."
            ) from exc

    connections = await connections_service.list_connections(db, user_id=user_id)
    if not connections:
        return None
    active = [item for item in connections if item.is_active]
    return (active or connections)[0]


def _resolve_country(raw: str | None, connection: BrokerConnection) -> str:
    """Pick the country to read, defaulting to the connection's own value."""
    candidate = raw or connection.country or "argentina"
    try:
        return connections_service.validate_country(candidate)
    except connections_service.ConnectionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


async def _render_page(
    request: Request,
    auth: AuthContext,
    db: AsyncSession,
    *,
    kind: str,
    template_name: str,
    connection_id: int | None,
) -> HTMLResponse:
    """Load the selected connection and its newest snapshot, then render."""
    connection = await _select_connection(
        db, user_id=auth.user.id, connection_id=connection_id
    )
    connections = await connections_service.list_connections(db, user_id=auth.user.id)

    snapshot = None
    if connection is not None:
        snapshot = await broker_service.load_snapshot(
            db, user_id=auth.user.id, connection=connection, kind=kind
        )

    context = {
        "user": auth.user,
        "csrf_token": auth.session.csrf_token,
        "connection": connection,
        "connections": connections,
        "refresh": request.query_params.get("refresh"),
        "snapshot": snapshot,
        "iol_usage": call_budget.usage,
    }

    # The templates only receive fields they are meant to show. The raw
    # payload stays out of the context.
    if kind == SNAPSHOT_PROFILE:
        from app.infrastructure.iol.schemas import Profile

        context["profile"] = (
            Profile.model_validate(snapshot.payload) if snapshot else None
        )
    else:
        from app.infrastructure.iol.schemas import AccountStatus

        context["account"] = (
            AccountStatus.model_validate(snapshot.payload) if snapshot else None
        )

    return templates.TemplateResponse(request, template_name, context)


async def _refresh(
    auth: AuthContext,
    db: AsyncSession,
    *,
    kind: str,
    page_path: str,
    connection_id: int | None,
) -> RedirectResponse:
    """Refresh one snapshot and return to the page with a status code."""
    connection = await _select_connection(
        db, user_id=auth.user.id, connection_id=connection_id
    )
    if connection is None:
        return RedirectResponse(page_path, status_code=status.HTTP_303_SEE_OTHER)

    if kind == SNAPSHOT_PROFILE:
        result = await broker_service.read_profile(
            db, user_id=auth.user.id, connection=connection
        )
    else:
        result = await broker_service.read_account_status(
            db, user_id=auth.user.id, connection=connection
        )

    if result.ok:
        outcome = "ok"
    elif result.is_stale:
        outcome = "stale"
    else:
        outcome = "failed"

    location = f"{page_path}?connection_id={connection.id}&refresh={outcome}"
    return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    auth: AuthContext = Depends(require_user),
    connection_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Show the newest saved IOL profile snapshot."""
    return await _render_page(
        request,
        auth,
        db,
        kind=SNAPSHOT_PROFILE,
        template_name="profile.html",
        connection_id=connection_id,
    )


@router.post("/profile/refresh")
async def profile_refresh(
    auth: AuthContext = Depends(require_user),
    connection_id: int | None = Form(default=None),
    csrf_token: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Read the profile from IOL and save a new snapshot."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)
    return await _refresh(
        auth,
        db,
        kind=SNAPSHOT_PROFILE,
        page_path="/profile",
        connection_id=connection_id,
    )


@router.get("/account-status", response_class=HTMLResponse)
async def account_status_page(
    request: Request,
    auth: AuthContext = Depends(require_user),
    connection_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Show the newest saved account-status snapshot."""
    return await _render_page(
        request,
        auth,
        db,
        kind=SNAPSHOT_ACCOUNT_STATUS,
        template_name="account_status.html",
        connection_id=connection_id,
    )


@router.post("/account-status/refresh")
async def account_status_refresh(
    auth: AuthContext = Depends(require_user),
    connection_id: int | None = Form(default=None),
    csrf_token: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Read the account status from IOL and save a new snapshot."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)
    return await _refresh(
        auth,
        db,
        kind=SNAPSHOT_ACCOUNT_STATUS,
        page_path="/account-status",
        connection_id=connection_id,
    )


# --- Country portfolio ----------------------------------------------------


@router.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(
    request: Request,
    auth: AuthContext = Depends(require_user),
    connection_id: int | None = None,
    country: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Show the newest saved country-portfolio snapshot."""
    connection = await _select_connection(
        db, user_id=auth.user.id, connection_id=connection_id
    )
    connections = await connections_service.list_connections(db, user_id=auth.user.id)

    selected = country
    snapshot = None
    portfolio = None
    if connection is not None:
        selected = _resolve_country(country, connection)
        snapshot = await broker_service.load_snapshot(
            db,
            user_id=auth.user.id,
            connection=connection,
            kind=portfolio_snapshot_kind(selected),
        )
        if snapshot is not None:
            portfolio = Portfolio.model_validate(snapshot.payload)

    return templates.TemplateResponse(
        request,
        "portfolio.html",
        {
            "user": auth.user,
            "csrf_token": auth.session.csrf_token,
            "connection": connection,
            "connections": connections,
            "country": selected,
            "countries": connections_service.SUPPORTED_COUNTRIES,
            "portfolio": portfolio,
            "refresh": request.query_params.get("refresh"),
            "snapshot": snapshot,
            "iol_usage": call_budget.usage,
        },
    )


@router.post("/portfolio/refresh")
async def portfolio_refresh(
    auth: AuthContext = Depends(require_user),
    connection_id: int | None = Form(default=None),
    country: str = Form(default="argentina"),
    csrf_token: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Read one country portfolio from IOL and save a new snapshot."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)

    connection = await _select_connection(
        db, user_id=auth.user.id, connection_id=connection_id
    )
    if connection is None:
        return RedirectResponse("/portfolio", status_code=status.HTTP_303_SEE_OTHER)

    selected = _resolve_country(country, connection)
    result = await broker_service.read_portfolio(
        db, user_id=auth.user.id, connection=connection, country=selected
    )
    if result.ok:
        outcome = "ok"
    elif result.is_stale:
        outcome = "stale"
    else:
        outcome = "failed"

    location = (
        f"/portfolio?connection_id={connection.id}&country={selected}&refresh={outcome}"
    )
    return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)
