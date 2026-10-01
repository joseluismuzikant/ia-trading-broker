"""Help page: how the application works, plus a plain-language glossary.

The page is public. It renders for a signed-in user or for a visitor, so the
glossary can be read before logging in.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.deps import AuthContext, get_optional_auth
from app.templating import templates

router = APIRouter(tags=["pages"])


@router.get("/help", response_class=HTMLResponse)
async def help_page(
    request: Request,
    auth: AuthContext | None = Depends(get_optional_auth),
) -> HTMLResponse:
    """Render the help and glossary page."""
    return templates.TemplateResponse(
        request,
        "help.html",
        {
            "user": auth.user if auth is not None else None,
            "csrf_token": auth.session.csrf_token if auth is not None else "",
        },
    )
