"""Dashboard page."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.deps import AuthContext, require_user
from app.templating import templates

router = APIRouter(tags=["pages"])


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    auth: AuthContext = Depends(require_user),
) -> HTMLResponse:
    """Render the (empty) dashboard for the logged-in user."""
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": auth.user,
            "csrf_token": auth.session.csrf_token,
            "session_expires_at": auth.session.expires_at,
        },
    )
