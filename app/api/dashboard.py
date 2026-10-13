"""Dashboard page: where the account stands and what to do next."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import AuthContext, require_user
from app.domain.trading import Proposal
from app.services import analysis as analysis_service
from app.services import connections as connections_service
from app.services import orders as orders_service
from app.services import progress as progress_service
from app.templating import templates

router = APIRouter(tags=["pages"])

#: How many recent proposals the dashboard lists.
RECENT_PROPOSALS = 5


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    auth: AuthContext = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Render the signed-in user's overview. Every number is owner-scoped."""
    user_id = auth.user.id
    connections = await connections_service.list_connections(db, user_id=user_id)
    records = await analysis_service.list_all_proposals(db, user_id=user_id)
    orders = await orders_service.list_orders(db, user_id=user_id, limit=1000)

    awaiting = sum(1 for record in records if record.status == "pending_review")
    waiting = sum(1 for order in orders if order.status in analysis_service.UNSETTLED_ORDER_STATES)

    recent = []
    for record in records[:RECENT_PROPOSALS]:
        try:
            proposal = Proposal.model_validate(record.payload)
        except Exception:  # noqa: BLE001 - an unreadable payload must not hide the rest
            proposal = None
        recent.append(
            {
                "record": record,
                "model_label": proposal.advice.model_label if proposal and proposal.advice else "—",
                "trades": len(proposal.orders) if proposal else 0,
            }
        )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": auth.user,
            "csrf_token": auth.session.csrf_token,
            "session_expires_at": auth.session.expires_at,
            "connection_count": len(connections),
            "proposal_count": len(records),
            "awaiting_decision": awaiting,
            "orders_waiting": waiting,
            "recent": recent,
            "next_action": progress_service.next_action(
                connections=len(connections),
                proposals=len(records),
                awaiting_decision=awaiting,
                orders_waiting=waiting,
            ),
        },
    )
