"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api import analysis, auth, broker, connections, dashboard, health
from app.config import get_settings
from app.db import dispose_engine, init_db
from app.errors import CsrfError, RedirectToLogin
from app.templating import STATIC_DIR, templates

logger = logging.getLogger("ia_trading_broker")

settings = get_settings()

DESCRIPTION = (
    "Automatic trading application for InvertirOnline (IOL). "
    "Day 1 provides the project foundation, health checks, and login. "
    "Day 2 adds encrypted IOL connections, profile, and account status. "
    "Day 3 adds the country portfolio and market-data reads. "
    "Day 4 adds Python indicators, a paper ledger, and an immutable proposal."
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Validate configuration once when the process starts."""
    problems = settings.configuration_problems()
    if problems:
        for problem in problems:
            logger.warning("configuration problem: %s", problem)

    if settings.uses_insecure_encryption_key:
        logger.warning(
            "CREDENTIAL_ENCRYPTION_KEY is the public development key, so saved "
            "broker passwords are not really protected. Set a private key in "
            ".env before saving a real IOL connection."
        )

    logger.info(
        "%s %s starting (environment=%s)",
        settings.app_name,
        __version__,
        settings.environment,
    )
    try:
        await init_db()
        yield
    finally:
        await dispose_engine()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(connections.router)
    app.include_router(broker.router)
    app.include_router(analysis.router)

    register_exception_handlers(app)
    return app


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the HTML error handlers."""

    @app.exception_handler(RedirectToLogin)
    async def _redirect_to_login(_request: Request, exc: RedirectToLogin):
        return RedirectResponse(exc.next_url, status_code=303)

    @app.exception_handler(CsrfError)
    async def _csrf_error(request: Request, exc: CsrfError):
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": 400, "detail": exc.message},
            status_code=400,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException):
        # Redirects raised as HTTP exceptions must stay redirects.
        headers = getattr(exc, "headers", None) or {}
        location = next((v for k, v in headers.items() if k.lower() == "location"), None)
        if location and 300 <= exc.status_code < 400:
            return RedirectResponse(location, status_code=exc.status_code)

        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": exc.status_code, "detail": detail},
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> HTMLResponse:
        # Log the type only; never log request bodies or credentials.
        logger.exception("unhandled error on %s: %s", request.url.path, type(exc).__name__)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": 500, "detail": "An unexpected error occurred."},
            status_code=500,
        )


app = create_app()

