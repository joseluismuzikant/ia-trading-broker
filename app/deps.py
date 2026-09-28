"""Shared FastAPI dependencies and request helpers."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Form, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_db
from app.errors import CsrfError, RedirectToLogin
from app.models import Session, User
from app.security import generate_token, tokens_equal
from app.services import auth as auth_service

#: Cookie that carries the CSRF token for unauthenticated forms (login).
CSRF_COOKIE_NAME = "iatb_csrf"


@dataclass(frozen=True)
class AuthContext:
    """The authenticated user together with their server-side session."""

    user: User
    session: Session


def get_settings_dep() -> Settings:
    """FastAPI wrapper around the cached settings object."""
    return get_settings()


async def get_optional_auth(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> AuthContext | None:
    """Return the current auth context, or None when not logged in."""
    raw_token = request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        return None

    session = await auth_service.get_session_by_token(db, raw_token)
    if session is None:
        return None

    user = await auth_service.get_user_by_id(db, session.user_id)
    if user is None or not user.is_active:
        await auth_service.revoke_session(db, session)
        return None

    return AuthContext(user=user, session=session)


async def require_user(
    auth: AuthContext | None = Depends(get_optional_auth),
) -> AuthContext:
    """Return the auth context or redirect to the login page."""
    if auth is None:
        raise RedirectToLogin()
    return auth


def session_cookie_kwargs(settings: Settings) -> dict[str, object]:
    """Return the security attributes used for session cookies."""
    return {
        "path": "/",
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": "lax",
    }


def set_session_cookie(
    response: Response, settings: Settings, raw_token: str
) -> None:
    """Attach the session cookie to a response."""
    response.set_cookie(
        settings.session_cookie_name,
        raw_token,
        max_age=settings.session_ttl_hours * 3600,
        **session_cookie_kwargs(settings),
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    """Remove the session cookie from a response."""
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def get_csrf_token(request: Request) -> str:
    """Return (and remember) the CSRF token for an unauthenticated form.

    The token is stored in a readable double-submit cookie and rendered into
    the form. On submit, both halves must match.
    """
    return request.cookies.get(CSRF_COOKIE_NAME) or generate_token()


def set_csrf_cookie(token: str, response: Response, settings: Settings) -> None:
    """Attach the double-submit CSRF cookie to a response."""
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        path="/",
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def validate_csrf_pair(
    *,
    submitted: str | None,
    cookie_value: str | None = None,
    expected: str | None = None,
) -> None:
    """Raise :class:`CsrfError` when a submitted CSRF token is not valid.

    When ``expected`` is given (an authenticated session) the submitted token
    must match it. Otherwise the double-submit cookie is compared.
    """
    if not submitted:
        raise CsrfError("missing CSRF token")

    reference = expected or cookie_value
    if not reference or not tokens_equal(submitted, reference):
        raise CsrfError("invalid CSRF token")


async def validate_form_csrf(
    request: Request,
    auth: AuthContext,
    csrf_token: str | None = Form(default=None),
) -> None:
    """Dependency-friendly CSRF check for authenticated forms."""
    validate_csrf_pair(submitted=csrf_token, expected=auth.session.csrf_token)
