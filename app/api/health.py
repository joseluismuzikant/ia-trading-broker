"""Health check endpoints.

``/healthz`` is a liveness probe: it only proves the process is running.
``/readyz`` is a readiness probe: it checks the database connection and the
application configuration only. It deliberately does not call the broker.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import get_db
from app.deps import get_settings_dep

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
async def liveness() -> JSONResponse:
    """Return OK as long as the process can answer requests."""
    return JSONResponse({"status": "ok"})


@router.get("/readyz", summary="Readiness probe")
async def readiness(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> JSONResponse:
    """Check database connectivity and configuration."""
    database_ok = True
    database_error: str | None = None
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        database_ok = False
        database_error = type(exc).__name__

    config_problems = settings.configuration_problems()
    ready = database_ok and not config_problems

    body: dict[str, object] = {
        "status": "ready" if ready else "not_ready",
        "checks": {
            "database": {
                "ok": database_ok,
                "error": database_error,
            },
            "configuration": {
                "ok": not config_problems,
                "problems": config_problems,
            },
        },
    }
    return JSONResponse(body, status_code=200 if ready else 503)
