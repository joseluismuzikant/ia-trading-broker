"""Broker connection pages: list, create, test, and delete."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import AuthContext, require_user, validate_csrf_pair
from app.domain import brokers as broker_kinds
from app.infrastructure.iol import call_budget
from app.models import (
    EVENT_LEDGER,
    SNAPSHOT_ACCOUNT_STATUS,
    SNAPSHOT_PROFILE,
    BrokerConnection,
)
from app.services import analysis as analysis_service
from app.services import connections as connections_service
from app.services import events as events_service
from app.services import ledger as ledger_service
from app.services import snapshots as snapshots_service
from app.services.broker import test_connection
from app.templating import templates

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


def _form_context(auth: AuthContext, *, kind, sources, error=None, form=None) -> dict:
    """What the new-connection page needs: the brokers, the chosen one, its fields."""
    return {
        **_base_context(auth),
        "brokers": list(broker_kinds.BROKERS.values()),
        "kind": kind,
        "countries": connections_service.SUPPORTED_COUNTRIES,
        "sources": sources,
        "error": error,
        "form": form or {},
    }


async def _price_sources(db: AsyncSession, auth: AuthContext) -> list[BrokerConnection]:
    """The user's own real connections, which a paper account may take prices from."""
    owned = await connections_service.list_connections(db, user_id=auth.user.id)
    return [item for item in owned if not broker_kinds.is_simulated(item.broker)]


@router.get("/connections/new", response_class=HTMLResponse)
async def new_connection_form(
    request: Request,
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Render the form for the chosen broker (IOL unless another is picked)."""
    kind = (
        broker_kinds.kind_of(request.query_params.get("broker", "")) or broker_kinds.BROKERS["iol"]
    )
    return templates.TemplateResponse(
        request,
        "connection_new.html",
        _form_context(
            auth,
            kind=kind,
            sources=await _price_sources(db, auth),
            error=request.query_params.get("error"),
        ),
    )


@router.post("/connections", response_class=HTMLResponse)
async def create_connection(
    request: Request,
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Encrypt and save a new connection, then open its detail page.

    The fields asked for depend on the broker, so the form is read as a whole.
    A password or key is never echoed back when the form is shown again.
    """
    form = await request.form()
    validate_csrf_pair(submitted=str(form.get("csrf_token", "")), expected=auth.session.csrf_token)

    broker = str(form.get("broker", broker_kinds.IOL)).strip().lower() or broker_kinds.IOL
    kind = broker_kinds.kind_of(broker)
    label = str(form.get("label", ""))
    country = str(form.get("country", "argentina"))

    try:
        if kind is None:
            raise connections_service.ConnectionValidationError(f"unknown broker {broker!r}")
        if broker == broker_kinds.IOL:
            connection = await connections_service.create_connection(
                db,
                user_id=auth.user.id,
                label=label,
                username=str(form.get("username", "")),
                password=str(form.get("password", "")),
                country=country,
            )
        else:
            connection = await _create_other_broker(db, auth, kind, form, label, country)
    except (
        connections_service.ConnectionValidationError,
        connections_service.ConnectionAlreadyExistsError,
    ) as exc:
        # A refused insert rolls the transaction back, which expires the rows
        # the page header renders. Reload them before building the response,
        # otherwise rendering triggers a lazy load outside the async context.
        await db.refresh(auth.user)
        await db.refresh(auth.session)

        # Re-render the form. Secret fields are deliberately left empty.
        shown = {"label": label, "country": country}
        for spec in kind.fields if kind else ():
            if not spec.secret:
                shown[spec.name] = str(form.get(spec.name, ""))
        for name in ("username", "starting", "initial_cash", "price_source"):
            if name != "username" or broker == broker_kinds.IOL:
                shown[name] = str(form.get(name, ""))
        return templates.TemplateResponse(
            request,
            "connection_new.html",
            _form_context(
                auth,
                kind=kind or broker_kinds.BROKERS["iol"],
                sources=await _price_sources(db, auth),
                error=str(exc),
                form=shown,
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return RedirectResponse(f"/connections/{connection.id}", status_code=status.HTTP_303_SEE_OTHER)


async def _create_other_broker(db, auth, kind, form, label: str, country: str) -> BrokerConnection:
    """Save a non-IOL connection from the submitted form."""
    fields = {spec.name: str(form.get(spec.name, "")) for spec in kind.fields}
    options: dict[str, str] = {}
    source_id: int | None = None
    if kind.simulated:
        raw_source = str(form.get("price_source", "")).strip()
        if not raw_source.isdigit():
            raise connections_service.ConnectionValidationError(
                "Pick a connection to take prices from."
            )
        source_id = int(raw_source)
        options["starting"] = str(form.get("starting", "cash")).strip() or "cash"
        options["initial_cash"] = str(form.get("initial_cash", "")).strip()
    connection = await connections_service.create_broker_connection(
        db,
        user_id=auth.user.id,
        broker=kind.key,
        label=label,
        fields=fields,
        options=options,
        country=country,
        price_source_connection_id=source_id,
    )
    if kind.simulated:
        try:
            await ledger_service.reset_paper_account(
                db,
                user_id=auth.user.id,
                connection=connection,
                universe=analysis_service.load_config(),
            )
        except ledger_service.LedgerError as exc:
            # No half-made account: a paper connection without a state is useless.
            await connections_service.delete_connection(
                db, user_id=auth.user.id, connection_id=connection.id
            )
            raise connections_service.ConnectionValidationError(str(exc)) from exc
    return connection


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
    paper_account = None
    if broker_kinds.is_simulated(connection.broker):
        paper_account = await ledger_service.load_paper_account(
            db, user_id=auth.user.id, connection=connection, country=connection.country
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
            "kind": broker_kinds.kind_of(connection.broker),
            "paper_account": paper_account,
            "reset_result": request.query_params.get("reset"),
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


@router.post("/connections/{connection_id}/reset")
async def reset_paper_connection(
    connection_id: int,
    auth: AuthContext = Depends(require_user),
    csrf_token: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Return a paper account to its starting point. Only a paper account has one."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)

    connection = await _get_owned_connection(db, auth, connection_id)
    if not broker_kinds.is_simulated(connection.broker):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only a paper account can be reset.",
        )
    try:
        await ledger_service.reset_paper_account(
            db,
            user_id=auth.user.id,
            connection=connection,
            universe=analysis_service.load_config(),
        )
    except (ledger_service.LedgerError, connections_service.ConnectionNotFoundError):
        return RedirectResponse(
            f"/connections/{connection_id}?reset=failed", status_code=status.HTTP_303_SEE_OTHER
        )
    await events_service.record_event(
        db,
        user_id=auth.user.id,
        connection_id=connection.id,
        category=EVENT_LEDGER,
        event_type="paper_account.reset",
        message="The paper account was returned to its starting point.",
        mode="PAPER",
    )
    return RedirectResponse(
        f"/connections/{connection_id}?reset=ok", status_code=status.HTTP_303_SEE_OTHER
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
