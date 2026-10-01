"""The trading history page.

It reads the local event log and the saved orders. It never calls the broker, so
it works even when IOL is unavailable. Every row is scoped to the signed-in user.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.broker import _select_connection
from app.db import get_db
from app.deps import AuthContext, require_user
from app.services import connections as connections_service
from app.services import events as events_service
from app.services import orders as orders_service
from app.templating import templates

router = APIRouter(tags=["history"])


@router.get("/history", response_class=HTMLResponse)
async def history_page(
    request: Request,
    auth: AuthContext = Depends(require_user),
    connection_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Show the newest trading events and orders for the user."""
    connection = await _select_connection(
        db, user_id=auth.user.id, connection_id=connection_id
    )
    connections = await connections_service.list_connections(db, user_id=auth.user.id)
    scoped_connection_id = connection.id if connection is not None else None

    events = await events_service.list_events(
        db, user_id=auth.user.id, connection_id=scoped_connection_id, limit=200
    )
    orders = await orders_service.list_orders(
        db, user_id=auth.user.id, connection_id=scoped_connection_id, limit=100
    )

    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "user": auth.user,
            "csrf_token": auth.session.csrf_token,
            "connection": connection,
            "connections": connections,
            "events": events,
            "orders": orders,
        },
    )
