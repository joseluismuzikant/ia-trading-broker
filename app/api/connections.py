"""Broker connection pages: list, create, test, and delete."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import AuthContext, require_user, validate_csrf_pair
from app.infrastructure.iol import call_budget
from app.services import connections as connections_service
from app.services import snapshots as snapshots_service
from app.services.broker import test_connection
from app.templating import templates
from app.models import (
    SNAPSHOT_ACCOUNT_STATUS,
    SNAPSHOT_PROFILE,
    BrokerConnection,
)

logger = logging.getLogger("ia_trading_broker.connections")

router = APIRouter(tags=["connections"])


def _base_context(auth: AuthContext) -> dict:
    """Context shared by every page that renders the site header."""
    return {"user": auth.user, "csrf_token": auth.session.csrf_token}


async def _get_owned_connection(
    db: AsyncSession, auth: AuthContext, connection_id: int
) -> BrokerConnection:
    """Load a connection, or answer 404 when it is not this user's."""
    try:
        return await connections_service.get_connection(
            db, user_id=auth.user.id, connection_id=connection_id
        )
    except connections_service.ConnectionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found."
        ) from exc


@router.get("/connections", response_class=HTMLResponse)
async def list_connections(
    request: Request,
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Show every saved broker connection for the logged-in user."""
    connections = await connections_service.list_connections(db, user_id=auth.user.id)
    return templates.TemplateResponse(
        request,
        "connections.html",
        {**_base_context(auth), "connections": connections},
    )


@router.get("/connections/new", response_class=HTMLResponse)
async def new_connection_form(
    request: Request,
    auth: AuthContext = Depends(require_user),
) -> HTMLResponse:
    """Render the form used to save an IOL login."""
    return templates.TemplateResponse(
        request,
        "connection_new.html",
        {
            **_base_context(auth),
            "countries": connections_service.SUPPORTED_COUNTRIES,
            "error": request.query_params.get("error"),
            "form": {},
        },
    )


@router.post("/connections", response_class=HTMLResponse)
async def create_connection(
    request: Request,
    auth: AuthContext = Depends(require_user),
    label: str = Form(default=""),
    username: str = Form(default=""),
    password: str = Form(default=""),
    country: str = Form(default="argentina"),
    csrf_token: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Encrypt and save a new connection, then open its detail page."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)

    try:
        connection = await connections_service.create_connection(
            db,
            user_id=auth.user.id,
            label=label,
            username=username,
            password=password,
            country=country,
        )
    except (
        connections_service.ConnectionValidationError,
        connections_service.ConnectionAlreadyExistsError,
    ) as exc:
        # A refused insert rolls the transaction back, which expires the rows
        # the page header renders. Reload them before building the response,
        # otherwise rendering triggers a lazy load outside the async context.
        await db.refresh(auth.user)
        await db.refresh(auth.session)

        # Re-render the form. The password field is deliberately left empty.
        return templates.TemplateResponse(
            request,
            "connection_new.html",
            {
                **_base_context(auth),
                "countries": connections_service.SUPPORTED_COUNTRIES,
                "error": str(exc),
                "form": {"label": label, "username": username, "country": country},
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return RedirectResponse(
        f"/connections/{connection.id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/connections/{connection_id}", response_class=HTMLResponse)
async def connection_detail(
    connection_id: int,
    request: Request,
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Show one connection without ever revealing the saved password."""
    connection = await _get_owned_connection(db, auth, connection_id)
    profile_snapshot = await snapshots_service.latest_snapshot(
        db,
        user_id=auth.user.id,
        connection_id=connection.id,
        kind=SNAPSHOT_PROFILE,
    )
    account_snapshot = await snapshots_service.latest_snapshot(
        db,
        user_id=auth.user.id,
        connection_id=connection.id,
        kind=SNAPSHOT_ACCOUNT_STATUS,
    )
    return templates.TemplateResponse(
        request,
        "connection_detail.html",
        {
            **_base_context(auth),
            "connection": connection,
            "profile_snapshot": profile_snapshot,
            "account_snapshot": account_snapshot,
            "test_result": request.query_params.get("test"),
            "iol_usage": call_budget.usage,
        },
    )


@router.post("/connections/{connection_id}/test")
async def test_saved_connection(
    connection_id: int,
    auth: AuthContext = Depends(require_user),
    csrf_token: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Ask IOL for a token to prove the saved login still works."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)

    connection = await _get_owned_connection(db, auth, connection_id)
    result = await test_connection(db, connection=connection)
    outcome = "ok" if result.ok else "failed"
    return RedirectResponse(
        f"/connections/{connection_id}?test={outcome}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/connections/{connection_id}/delete")
async def delete_saved_connection(
    connection_id: int,
    auth: AuthContext = Depends(require_user),
    csrf_token: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Delete a connection together with its snapshots."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)

    await _get_owned_connection(db, auth, connection_id)
    await connections_service.delete_connection(
        db, user_id=auth.user.id, connection_id=connection_id
    )
    return RedirectResponse("/connections", status_code=status.HTTP_303_SEE_OTHER)
