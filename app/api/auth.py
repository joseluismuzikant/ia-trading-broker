"""Login, logout, and root redirect routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import get_db
from app.deps import (
    AuthContext,
    clear_session_cookie,
    get_csrf_token,
    get_optional_auth,
    get_settings_dep,
    require_user,
    set_csrf_cookie,
    set_session_cookie,
    validate_csrf_pair,
)
from app.errors import CsrfError
from app.services import auth as auth_service
from app.templating import templates

router = APIRouter(tags=["auth"])

#: Deliberately generic message so callers cannot distinguish the failure cause.
INVALID_CREDENTIALS_MESSAGE = "Invalid username or password."


@router.get("/", include_in_schema=False)
async def index(
    auth: AuthContext | None = Depends(get_optional_auth),
) -> RedirectResponse:
    """Send the browser to the dashboard or the login page."""
    target = "/dashboard" if auth is not None else "/login"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request,
    auth: AuthContext | None = Depends(get_optional_auth),
    settings: Settings = Depends(get_settings_dep),
) -> HTMLResponse:
    """Render the login page."""
    if auth is not None:
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    csrf_token = get_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "login.html",
        {"csrf_token": csrf_token, "error": None},
    )
    set_csrf_cookie(csrf_token, response, settings)
    return response


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    csrf_token: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> HTMLResponse:
    """Validate credentials, create a session, and set the cookie."""
    # --- CSRF check (double-submit cookie, pre-authentication) -----------
    try:
        validate_csrf_pair(
            submitted=csrf_token,
            cookie_value=request.cookies.get("iatb_csrf"),
        )
    except CsrfError:
        fresh_token = get_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf_token": fresh_token,
                "error": "Your session expired. Please try again.",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
        set_csrf_cookie(fresh_token, response, settings)
        return response

    user = await auth_service.authenticate(db, username=username, password=password)
    if user is None:
        fresh_token = get_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "login.html",
            {"csrf_token": fresh_token, "error": INVALID_CREDENTIALS_MESSAGE},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        set_csrf_cookie(fresh_token, response, settings)
        return response

    raw_token, _session = await auth_service.create_session(
        db,
        user=user,
        ttl_hours=settings.session_ttl_hours,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )

    response = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    # Always issue a brand new session cookie and drop the pre-auth CSRF cookie
    # so a fixed session cannot be reused after login.
    set_session_cookie(response, settings, raw_token)
    response.delete_cookie("iatb_csrf", path="/")
    return response


@router.post("/logout")
async def logout(
    request: Request,
    auth: AuthContext = Depends(require_user),
    csrf_token: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> RedirectResponse:
    """Revoke the current session, clear the cookie, and return to login."""
    try:
        validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)
    except CsrfError:
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    await auth_service.revoke_session(db, auth.session)
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response, settings)
    return response

